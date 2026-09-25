"""Independent Redis Worker for durable Agent-Team Run execution.

Start with::

    python -m app.worker

The API process only creates Runs and publishes commands in Redis mode. This
worker owns provider calls, LangGraph execution, renewable leases, and control
commands while SQLite remains the audit and recovery authority.
"""

from __future__ import annotations

import asyncio
import os
import socket
import uuid
from contextlib import suppress
from pathlib import Path
from typing import Any

import aiosqlite
from dotenv import load_dotenv
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from .agents.registry import AgentRegistry
from .llm.factory import ProviderFactory
from .repositories.sqlite import SQLiteRepository
from .runtime.coordination import ClaimedJob, RedisRunCoordinator, RunLease, create_run_coordinator_from_env
from .services.artifacts import ArtifactService
from .services.workflow_service import WorkflowService
from .skills.registry import SkillRegistry
from .workflow.events import WorkflowEventBus
from .workflow.executor import WorkflowExecutor
from .workflow.models import RunStatus, utc_now


BASE_DIR = Path(__file__).resolve().parent
BACKEND_DIR = BASE_DIR.parent
load_dotenv(BACKEND_DIR / ".env", override=False)
load_dotenv(BACKEND_DIR / ".env.runtime", override=False)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


class AgentTeamWorker:
    def __init__(
        self,
        coordinator: RedisRunCoordinator,
        repository: SQLiteRepository,
        workflows: WorkflowService,
        executor: WorkflowExecutor,
        event_bus: WorkflowEventBus,
        *,
        worker_id: str | None = None,
        lease_seconds: int = 30,
        max_auto_recoveries: int = 5,
        max_concurrency: int = 4,
    ) -> None:
        self.coordinator = coordinator
        self.repository = repository
        self.workflows = workflows
        self.executor = executor
        self.event_bus = event_bus
        self.worker_id = worker_id or f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        self.lease_seconds = max(10, int(lease_seconds))
        self.heartbeat_seconds = max(2, self.lease_seconds // 3)
        self.max_auto_recoveries = max(1, int(max_auto_recoveries))
        self.max_concurrency = max(1, int(max_concurrency))
        self._stopping = False
        self._running_tasks: set[asyncio.Task[bool]] = set()

    async def reconcile(self) -> None:
        """Re-enqueue durable non-terminal Runs after a process outage."""
        for run in self.repository.list_recoverable_runs():
            run_id = str(run.get("id") or "")
            if not run_id or await self.coordinator.lease_owner(run_id):
                continue
            snapshot = run.get("state") if isinstance(run.get("state"), dict) else {}
            waiting_step_id = snapshot.get("waiting_step_id")
            if int(run.get("recovery_count") or 0) >= self.max_auto_recoveries and not waiting_step_id:
                message = f"Run 自动恢复已达到上限 {self.max_auto_recoveries} 次，需要人工重试"
                self.repository.update_run(
                    run_id,
                    status=RunStatus.FAILED.value,
                    execution_status=RunStatus.FAILED.value,
                    finished_at=utc_now(),
                    error_message=message,
                )
                await self.event_bus.emit("workflow.recovery_exhausted", run_id, {
                    "limit": self.max_auto_recoveries,
                    "reason": message,
                })
                continue
            await self.coordinator.enqueue(run_id, "recover", {"reason": "worker_startup_reconciliation"})

    async def _execute(self, job: ClaimedJob) -> None:
        run = self.repository.get_run(job.run_id)
        if not run:
            return
        run_status = str(run.get("status"))
        if job.action == "retry" and run_status not in {RunStatus.FAILED.value, RunStatus.STOPPED.value}:
            return
        if run_status in {RunStatus.SUCCESS.value, RunStatus.FAILED.value, RunStatus.STOPPED.value} and job.action != "retry":
            return
        workflow = self.workflows.get_workflow_for_run(run)
        inputs = run.get("inputs") if isinstance(run.get("inputs"), dict) else {}
        requirement = str(inputs.get("requirement") or "")
        runtime = inputs.get("runtime") if isinstance(inputs.get("runtime"), dict) else {}

        # Provider configuration may be changed through the API while this
        # long-lived Worker remains alive. Reload the non-secret process config
        # before every new Run; keys never enter Redis payloads.
        load_dotenv(BACKEND_DIR / ".env", override=True)
        provider = ProviderFactory.create_from_env()
        self.executor.provider = provider
        self.executor.model_invocation.set_provider(provider)
        if job.action == "retry":
            await self.executor.retry(job.run_id, workflow)
        elif job.action == "approve":
            await self.executor.resume_persisted(job.run_id, workflow)
            await self.executor.approve(job.run_id, str(job.payload.get("decision") or ""))
            await self.executor.wait_for_control_resolution(job.run_id)
        elif job.action == "clarify":
            await self.executor.resume_persisted(job.run_id, workflow)
            answers = job.payload.get("answers") if isinstance(job.payload.get("answers"), dict) else {}
            await self.executor.clarify(job.run_id, answers)
            await self.executor.wait_for_control_resolution(job.run_id)
        elif str(run.get("status")) in {RunStatus.PENDING.value, RunStatus.QUEUED.value} and not run.get("state"):
            await self.executor.start(job.run_id, workflow, {"requirement": requirement}, runtime)
        else:
            await self.executor.resume_persisted(job.run_id, workflow)

    async def _apply_control(self, run_id: str, lease: RunLease, control: dict[str, Any]) -> None:
        action = str(control.get("action") or "")
        payload = control.get("payload") if isinstance(control.get("payload"), dict) else {}
        if str(payload.get("lease_token") or "") != lease.token:
            return
        try:
            if action == "approve":
                await self.executor.approve(run_id, str(payload.get("decision") or ""))
            elif action == "clarify":
                answers = payload.get("answers") if isinstance(payload.get("answers"), dict) else {}
                await self.executor.clarify(run_id, answers)
            elif action == "stop":
                await self.executor.stop(run_id)
            else:
                return
            await self.event_bus.emit("worker.control_applied", run_id, {
                "workerId": self.worker_id, "action": action, "controlId": control.get("id"),
            })
        except ValueError as exc:
            await self.event_bus.emit("worker.control_rejected", run_id, {
                "workerId": self.worker_id, "action": action, "controlId": control.get("id"),
                "reason": str(exc),
            })

    async def _run_claimed(self, job: ClaimedJob) -> bool:
        run = self.repository.get_run(job.run_id)
        if not run:
            await self.coordinator.ack_job(job.message_id)
            return True
        run_status = str(run.get("status"))
        if job.action == "retry" and run_status not in {RunStatus.FAILED.value, RunStatus.STOPPED.value}:
            await self.coordinator.ack_job(job.message_id)
            return True
        if run_status in {RunStatus.SUCCESS.value, RunStatus.FAILED.value, RunStatus.STOPPED.value} and job.action != "retry":
            await self.coordinator.ack_job(job.message_id)
            return True

        lease = await self.coordinator.acquire_lease(job.run_id, self.worker_id, self.lease_seconds)
        if lease is None:
            return False
        self.repository.record_worker_lease(job.run_id, self.worker_id, lease.token)
        await self.event_bus.emit("worker.lease_acquired", job.run_id, {
            "workerId": self.worker_id, "leaseToken": lease.token[:8], "action": job.action,
        })
        with self.repository.lease_fence(job.run_id, lease.token):
            execution = asyncio.create_task(self._execute(job))
        control_cursor = "0-0"
        last_heartbeat = 0.0
        release_reason = "completed"
        try:
            while not self._stopping:
                now = asyncio.get_running_loop().time()
                if now - last_heartbeat >= self.heartbeat_seconds:
                    await self.coordinator.heartbeat_worker(self.worker_id, self.lease_seconds)
                    if not await self.coordinator.renew_lease(lease, self.lease_seconds):
                        release_reason = "lease_lost"
                        execution.cancel()
                        with suppress(BaseException):
                            await execution
                        await self.executor.abandon_for_lease_loss(job.run_id)
                        await self.event_bus.emit("worker.lease_lost", job.run_id, {"workerId": self.worker_id})
                        return False
                    with self.repository.lease_fence(job.run_id, lease.token):
                        self.repository.update_run(job.run_id, heartbeat_at=utc_now())
                    last_heartbeat = now

                control_cursor, controls = await self.coordinator.read_controls(
                    job.run_id, control_cursor, block_ms=500
                )
                for control in controls:
                    with self.repository.lease_fence(job.run_id, lease.token):
                        await self._apply_control(job.run_id, lease, control)

                if execution.done():
                    with suppress(asyncio.CancelledError):
                        await execution
                current = self.repository.get_run(job.run_id) or {}
                if str(current.get("status")) in {
                    RunStatus.WAITING_APPROVAL.value,
                    RunStatus.WAITING_CLARIFICATION.value,
                }:
                    # The executor persists the waiting status before it emits
                    # the workflow-level pause event.  Releasing the lease in
                    # that tiny window cancels the executor between
                    # ``step.waiting_*`` and ``workflow.waiting_*`` and leaves
                    # the UI without enough information to open its modal.
                    # Let the executor reach its stable interrupted checkpoint
                    # before detaching this Worker.
                    if not execution.done():
                        await asyncio.sleep(0.05)
                        continue
                    release_reason = "paused_for_user"
                    await self.executor.abandon_for_lease_loss(job.run_id)
                    await self.coordinator.ack_job(job.message_id)
                    return True
                if str(current.get("status")) in {RunStatus.SUCCESS.value, RunStatus.FAILED.value, RunStatus.STOPPED.value}:
                    await self.coordinator.ack_job(job.message_id)
                    return True
                await asyncio.sleep(0.1)
            release_reason = "worker_shutdown"
            return False
        except ValueError as exc:
            # Invalid/stale control jobs are deterministic. Leaving them in
            # the Redis pending list causes infinite redelivery and repeated
            # "no recoverable node" errors, so record and acknowledge them.
            release_reason = f"job_rejected:{type(exc).__name__}"
            if not execution.done():
                execution.cancel()
                with suppress(BaseException):
                    await execution
            await self.executor.abandon_for_lease_loss(job.run_id)
            await self.event_bus.emit("worker.execution_error", job.run_id, {
                "workerId": self.worker_id, "error": str(exc), "errorType": type(exc).__name__,
                "terminal": True,
            })
            await self.coordinator.ack_job(job.message_id)
            return True
        except Exception as exc:
            release_reason = f"worker_error:{type(exc).__name__}"
            if not execution.done():
                execution.cancel()
                with suppress(BaseException):
                    await execution
            await self.executor.abandon_for_lease_loss(job.run_id)
            await self.event_bus.emit("worker.execution_error", job.run_id, {
                "workerId": self.worker_id, "error": str(exc), "errorType": type(exc).__name__,
            })
            return False
        finally:
            self.repository.release_worker_lease(job.run_id, lease.token, release_reason)
            await self.coordinator.release_lease(lease)
            await self.event_bus.emit("worker.lease_released", job.run_id, {
                "workerId": self.worker_id, "reason": release_reason,
            })

    async def run_forever(self) -> None:
        await self.coordinator.ping()
        await self.reconcile()
        try:
            while not self._stopping:
                await self.coordinator.heartbeat_worker(self.worker_id, self.lease_seconds)
                completed = {task for task in self._running_tasks if task.done()}
                for task in completed:
                    with suppress(BaseException):
                        task.result()
                self._running_tasks.difference_update(completed)
                if len(self._running_tasks) >= self.max_concurrency:
                    await asyncio.wait(
                        self._running_tasks,
                        timeout=1,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    continue
                job = await self.coordinator.claim_job(self.worker_id, block_ms=3000)
                if job is None:
                    continue
                task = asyncio.create_task(self._run_claimed(job))
                self._running_tasks.add(task)
        finally:
            for task in self._running_tasks:
                if not task.done():
                    task.cancel()
            if self._running_tasks:
                await asyncio.gather(*self._running_tasks, return_exceptions=True)
            self._running_tasks.clear()
            with suppress(Exception):
                await self.coordinator.remove_worker(self.worker_id)


async def _main() -> None:
    coordinator = create_run_coordinator_from_env()
    if coordinator is None:
        raise RuntimeError("REDIS_URL is required to start app.worker")
    repository = SQLiteRepository(BACKEND_DIR / "data" / "agent_team.db")
    workflows = WorkflowService(BASE_DIR / "workflows")
    event_bus = WorkflowEventBus(repository, coordinator)
    checkpoint_connection = await aiosqlite.connect(
        str(BACKEND_DIR / "data" / "langgraph_checkpoints.db"), timeout=30.0
    )
    await checkpoint_connection.execute("PRAGMA busy_timeout = 30000")
    await checkpoint_connection.execute("PRAGMA synchronous = NORMAL")
    await checkpoint_connection.commit()
    checkpointer = AsyncSqliteSaver(checkpoint_connection)
    await checkpointer.setup()
    executor = WorkflowExecutor(
        AgentRegistry.from_directory(BACKEND_DIR / "agents"),
        ProviderFactory.create_from_env(),
        event_bus,
        repository,
        checkpointer=checkpointer,
        artifact_service=ArtifactService(repository, BACKEND_DIR / "data" / "workspaces"),
        skill_registry=SkillRegistry.from_directories([
            os.getenv("AGENT_TEAM_SKILLS_DIR"), BACKEND_DIR / "skills",
        ]),
    )
    worker = AgentTeamWorker(
        coordinator,
        repository,
        workflows,
        executor,
        event_bus,
        lease_seconds=_env_int("REDIS_LEASE_SECONDS", 30),
        max_auto_recoveries=_env_int("REDIS_MAX_AUTO_RECOVERIES", 5),
        max_concurrency=_env_int("REDIS_WORKER_CONCURRENCY", 4),
    )
    try:
        await worker.run_forever()
    finally:
        await checkpoint_connection.close()
        await coordinator.close()


if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass
