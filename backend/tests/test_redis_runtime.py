from __future__ import annotations

import asyncio
import os
import uuid
from dataclasses import dataclass

import pytest

from app.repositories.sqlite import SQLiteRepository
from app.runtime.coordination import ClaimedJob, RedisRunCoordinator, RunLease
from app.worker import AgentTeamWorker
from app.workflow.events import WorkflowEventBus
from app.workflow.models import RunStatus


class FakeCoordinator:
    def __init__(self, *, lose_lease: bool = False) -> None:
        self.lose_lease = lose_lease
        self.lease: RunLease | None = None
        self.acked: list[str] = []
        self.enqueued: list[tuple[str, str]] = []
        self.released: list[str] = []
        self.heartbeats: list[str] = []
        self.acquired = asyncio.Event()

    async def acquire_lease(self, run_id: str, worker_id: str, ttl_seconds: int) -> RunLease | None:
        if self.lease is not None:
            return None
        self.lease = RunLease(run_id, worker_id, f"token-{worker_id}")
        self.acquired.set()
        return self.lease

    async def renew_lease(self, lease: RunLease, ttl_seconds: int) -> bool:
        return not self.lose_lease and self.lease == lease

    async def release_lease(self, lease: RunLease) -> bool:
        if self.lease != lease:
            return False
        self.released.append(lease.token)
        self.lease = None
        return True

    async def ack_job(self, message_id: str) -> None:
        self.acked.append(message_id)

    async def read_controls(self, run_id: str, after_id: str, *, block_ms: int = 1000):
        await asyncio.sleep(0)
        return after_id, []

    async def lease_owner(self, run_id: str):
        if self.lease is None or self.lease.run_id != run_id:
            return None
        return {"worker_id": self.lease.worker_id, "token": self.lease.token}

    async def enqueue(self, run_id: str, action: str, payload=None) -> str:
        self.enqueued.append((run_id, action))
        return str(len(self.enqueued))

    async def heartbeat_worker(self, worker_id: str, ttl_seconds: int = 30) -> None:
        self.heartbeats.append(worker_id)


class ConcurrentCoordinator:
    def __init__(self) -> None:
        self.jobs: asyncio.Queue[ClaimedJob] = asyncio.Queue()
        self.leases: dict[str, RunLease] = {}
        self.acked: list[str] = []

    async def ping(self) -> bool:
        return True

    async def heartbeat_worker(self, worker_id: str, ttl_seconds: int = 30) -> None:
        return None

    async def remove_worker(self, worker_id: str) -> None:
        return None

    async def claim_job(self, worker_id: str, *, block_ms: int = 5000):
        try:
            return await asyncio.wait_for(self.jobs.get(), timeout=0.05)
        except asyncio.TimeoutError:
            return None

    async def acquire_lease(self, run_id: str, worker_id: str, ttl_seconds: int):
        if run_id in self.leases:
            return None
        lease = RunLease(run_id, worker_id, f"lease-{run_id}")
        self.leases[run_id] = lease
        return lease

    async def renew_lease(self, lease: RunLease, ttl_seconds: int) -> bool:
        return self.leases.get(lease.run_id) == lease

    async def release_lease(self, lease: RunLease) -> bool:
        return self.leases.pop(lease.run_id, None) == lease

    async def ack_job(self, message_id: str) -> None:
        self.acked.append(message_id)

    async def read_controls(self, run_id: str, after_id: str, *, block_ms: int = 1000):
        await asyncio.sleep(0)
        return after_id, []

    async def lease_owner(self, run_id: str):
        lease = self.leases.get(run_id)
        return {"worker_id": lease.worker_id, "token": lease.token} if lease else None

    async def enqueue(self, run_id: str, action: str, payload=None) -> str:
        message_id = f"{self.jobs.qsize() + 1}-0"
        await self.jobs.put(ClaimedJob(message_id, run_id, action, payload or {}))
        return message_id


@dataclass
class FakeExecutor:
    abandoned: list[str]

    async def abandon_for_lease_loss(self, run_id: str) -> None:
        self.abandoned.append(run_id)


class CompletingWorker(AgentTeamWorker):
    async def _execute(self, job: ClaimedJob) -> None:
        await asyncio.sleep(0)
        self.repository.update_run(
            job.run_id,
            status=RunStatus.SUCCESS.value,
            execution_status=RunStatus.SUCCESS.value,
        )


class BlockingWorker(AgentTeamWorker):
    async def _execute(self, job: ClaimedJob) -> None:
        while True:
            await asyncio.sleep(0.01)


class DispatchWorker(AgentTeamWorker):
    def __init__(self, *args, second_completed: asyncio.Event, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.second_completed = second_completed

    async def reconcile(self) -> None:
        return None

    async def _execute(self, job: ClaimedJob) -> None:
        if job.run_id == "run-waiting":
            self.repository.update_run(
                job.run_id,
                status=RunStatus.WAITING_APPROVAL.value,
                execution_status=RunStatus.WAITING_APPROVAL.value,
            )
            return
        self.repository.update_run(
            job.run_id,
            status=RunStatus.SUCCESS.value,
            execution_status=RunStatus.SUCCESS.value,
        )
        self.second_completed.set()


class OrderedPauseWorker(AgentTeamWorker):
    """Expose the status/event ordering window used by the real executor."""

    def __init__(self, *args, pause_event_emitted: asyncio.Event, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.pause_event_emitted = pause_event_emitted

    async def _execute(self, job: ClaimedJob) -> None:
        self.repository.update_run(
            job.run_id,
            status=RunStatus.WAITING_APPROVAL.value,
            execution_status=RunStatus.WAITING_APPROVAL.value,
        )
        await self.event_bus.emit("step.waiting_approval", job.run_id, {
            "stepId": "architecture_approval",
            "status": RunStatus.WAITING_APPROVAL.value,
        })
        await asyncio.sleep(0.05)
        await self.event_bus.emit("workflow.waiting_approval", job.run_id, {
            "stepId": "architecture_approval",
            "status": RunStatus.WAITING_APPROVAL.value,
        })
        self.pause_event_emitted.set()


def make_run(repository: SQLiteRepository, run_id: str, status: str = RunStatus.RUNNING.value) -> None:
    repository.create_run(run_id, "software-development", {"requirement": "test"}, status, "now")


@pytest.mark.asyncio
async def test_event_stream_replays_only_after_cursor_and_closes_at_terminal(tmp_path):
    repository = SQLiteRepository(tmp_path / "runs.db")
    make_run(repository, "run-events")
    bus = WorkflowEventBus(repository)
    first = await bus.emit("workflow.started", "run-events", {})
    second = await bus.emit("workflow.completed", "run-events", {"status": "SUCCESS"})
    repository.update_run("run-events", status=RunStatus.SUCCESS.value)

    replay = [event async for event in bus.stream("run-events", int(first["id"]))]

    assert [event["id"] for event in replay] == [second["id"]]


def test_attempt_and_worker_lease_audit_is_idempotent(tmp_path):
    repository = SQLiteRepository(tmp_path / "runs.db")
    make_run(repository, "run-audit")
    attempts = [{"usage": {"input_tokens": 12, "output_tokens": 34}, "finish_reason": "stop"}]
    repository.record_provider_attempts("run-audit", "frontend", attempts)
    repository.record_provider_attempts("run-audit", "frontend", attempts)
    repository.record_worker_lease("run-audit", "worker-a", "lease-a")
    repository.release_worker_lease("run-audit", "lease-a", "completed")

    assert len(repository.list_provider_attempts("run-audit")) == 1
    leases = repository.list_worker_leases("run-audit")
    assert len(leases) == 1
    assert leases[0]["release_reason"] == "completed"


@pytest.mark.asyncio
async def test_only_one_worker_can_execute_the_same_run(tmp_path):
    repository = SQLiteRepository(tmp_path / "runs.db")
    make_run(repository, "run-fenced")
    coordinator = FakeCoordinator()
    event_bus = WorkflowEventBus(repository)
    executor = FakeExecutor([])
    first = BlockingWorker(coordinator, repository, object(), executor, event_bus, worker_id="one")
    second = BlockingWorker(coordinator, repository, object(), executor, event_bus, worker_id="two")
    first_job = ClaimedJob("1-0", "run-fenced", "recover", {})
    second_job = ClaimedJob("2-0", "run-fenced", "recover", {})

    first_task = asyncio.create_task(first._run_claimed(first_job))
    await asyncio.wait_for(coordinator.acquired.wait(), timeout=1)
    assert await second._run_claimed(second_job) is False

    repository.update_run("run-fenced", status=RunStatus.SUCCESS.value)
    assert await asyncio.wait_for(first_task, timeout=1) is True
    assert coordinator.acked == ["1-0"]


@pytest.mark.asyncio
async def test_lease_loss_abandons_without_marking_run_failed(tmp_path):
    repository = SQLiteRepository(tmp_path / "runs.db")
    make_run(repository, "run-lease-loss")
    coordinator = FakeCoordinator(lose_lease=True)
    executor = FakeExecutor([])
    worker = BlockingWorker(
        coordinator,
        repository,
        object(),
        executor,
        WorkflowEventBus(repository),
        worker_id="failing-worker",
    )

    progressed = await worker._run_claimed(ClaimedJob("1-0", "run-lease-loss", "recover", {}))

    assert progressed is False
    assert executor.abandoned == ["run-lease-loss"]
    assert coordinator.acked == []
    assert repository.get_run("run-lease-loss")["status"] == RunStatus.RUNNING.value


@pytest.mark.asyncio
async def test_worker_reconciliation_requeues_unowned_runs(tmp_path):
    repository = SQLiteRepository(tmp_path / "runs.db")
    make_run(repository, "run-recover", RunStatus.PENDING.value)
    coordinator = FakeCoordinator()
    worker = CompletingWorker(
        coordinator,
        repository,
        object(),
        FakeExecutor([]),
        WorkflowEventBus(repository),
        worker_id="reconciler",
    )

    await worker.reconcile()

    assert coordinator.enqueued == [("run-recover", "recover")]


@pytest.mark.asyncio
async def test_worker_reconciliation_stops_non_progressing_recovery_loop(tmp_path):
    repository = SQLiteRepository(tmp_path / "runs.db")
    make_run(repository, "run-loop", RunStatus.RUNNING.value)
    repository.update_run("run-loop", recovery_count=5)
    coordinator = FakeCoordinator()
    worker = CompletingWorker(
        coordinator,
        repository,
        object(),
        FakeExecutor([]),
        WorkflowEventBus(repository),
        worker_id="reconciler",
        max_auto_recoveries=5,
    )

    await worker.reconcile()

    run = repository.get_run("run-loop")
    assert run["status"] == RunStatus.FAILED.value
    assert "达到上限" in run["error_message"]
    assert coordinator.enqueued == []


@pytest.mark.asyncio
async def test_waiting_approval_run_does_not_block_next_queued_run(tmp_path):
    repository = SQLiteRepository(tmp_path / "runs.db")
    make_run(repository, "run-waiting", RunStatus.PENDING.value)
    make_run(repository, "run-next", RunStatus.PENDING.value)
    coordinator = ConcurrentCoordinator()
    await coordinator.enqueue("run-waiting", "start")
    await coordinator.enqueue("run-next", "start")
    second_completed = asyncio.Event()
    worker = DispatchWorker(
        coordinator,
        repository,
        object(),
        FakeExecutor([]),
        WorkflowEventBus(repository),
        worker_id="concurrent-worker",
        max_concurrency=2,
        second_completed=second_completed,
    )

    worker_task = asyncio.create_task(worker.run_forever())
    await asyncio.wait_for(second_completed.wait(), timeout=1)
    assert repository.get_run("run-waiting")["status"] == RunStatus.WAITING_APPROVAL.value
    assert repository.get_run("run-next")["status"] == RunStatus.SUCCESS.value
    for _ in range(20):
        if "1-0" in coordinator.acked:
            break
        await asyncio.sleep(0.01)
    assert "1-0" in coordinator.acked
    assert "run-waiting" not in coordinator.leases
    worker._stopping = True
    await asyncio.wait_for(worker_task, timeout=1)


@pytest.mark.asyncio
async def test_worker_releases_waiting_run_only_after_workflow_pause_event(tmp_path):
    repository = SQLiteRepository(tmp_path / "runs.db")
    make_run(repository, "run-waiting", RunStatus.PENDING.value)
    coordinator = FakeCoordinator()
    pause_event_emitted = asyncio.Event()
    worker = OrderedPauseWorker(
        coordinator,
        repository,
        object(),
        FakeExecutor([]),
        WorkflowEventBus(repository),
        worker_id="ordered-worker",
        pause_event_emitted=pause_event_emitted,
    )

    progressed = await asyncio.wait_for(
        worker._run_claimed(ClaimedJob("1-0", "run-waiting", "start", {})),
        timeout=1,
    )

    assert progressed is True
    assert pause_event_emitted.is_set()
    assert [event["type"] for event in repository.list_events("run-waiting")][-2:] == [
        "workflow.waiting_approval",
        "worker.lease_released",
    ]


@pytest.mark.asyncio
async def test_stale_retry_job_is_acknowledged_without_reexecution(tmp_path):
    repository = SQLiteRepository(tmp_path / "runs.db")
    make_run(repository, "run-waiting", RunStatus.WAITING_APPROVAL.value)
    coordinator = FakeCoordinator()
    worker = BlockingWorker(
        coordinator,
        repository,
        object(),
        FakeExecutor([]),
        WorkflowEventBus(repository),
        worker_id="stale-retry-worker",
    )

    progressed = await worker._run_claimed(ClaimedJob("stale-1", "run-waiting", "retry", {}))

    assert progressed is True
    assert coordinator.acked == ["stale-1"]
    assert coordinator.lease is None


@pytest.mark.asyncio
@pytest.mark.skipif(not os.getenv("REDIS_TEST_URL"), reason="REDIS_TEST_URL is not configured")
async def test_real_redis_queue_events_worker_heartbeat_and_fenced_lease():
    prefix = f"agent-team-test-{uuid.uuid4().hex}"
    coordinator = RedisRunCoordinator(os.environ["REDIS_TEST_URL"], prefix=prefix, reclaim_idle_ms=5000)
    try:
        assert await coordinator.ping() is True
        message_id = await coordinator.enqueue("run-real", "start", {"source": "test"})
        claimed = await coordinator.claim_job("worker-one", block_ms=100)
        assert claimed is not None
        assert claimed.message_id == message_id
        assert claimed.run_id == "run-real"

        lease = await coordinator.acquire_lease("run-real", "worker-one", 10)
        assert lease is not None
        assert await coordinator.acquire_lease("run-real", "worker-two", 10) is None
        assert await coordinator.renew_lease(lease, 10) is True

        await coordinator.heartbeat_worker("worker-one", 10)
        workers = await coordinator.active_workers()
        assert workers == [{"worker_id": "worker-one"}]

        event = {"id": 9, "type": "step.completed", "runId": "run-real", "payload": {}}
        await coordinator.publish_event(event)
        stream = coordinator.stream_events("run-real", after_event_id=8)
        assert await asyncio.wait_for(anext(stream), timeout=1) == event
        await stream.aclose()

        assert await coordinator.release_lease(lease) is True
        await coordinator.ack_job(message_id)
        await coordinator.remove_worker("worker-one")
    finally:
        keys = [key async for key in coordinator.redis.scan_iter(match=f"{prefix}:*")]
        if keys:
            await coordinator.redis.delete(*keys)
        await coordinator.close()
