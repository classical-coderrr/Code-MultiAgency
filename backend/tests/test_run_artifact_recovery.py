import asyncio
import json

import pytest

from app.agents.registry import AgentRegistry
from app.llm.base import LLMError, LLMProvider, LLMResponse
from app.repositories.sqlite import SQLiteRepository
from app.workflow.events import WorkflowEventBus
from app.workflow.executor import WorkflowExecutor
from app.workflow.models import RunStatus, StepDefinition, StepStatus, StepType, WorkflowDefinition


class FailBackendOnceProvider(LLMProvider):
    def __init__(self, fail_backend: bool = True) -> None:
        self.calls: list[str] = []
        self.fail_backend = fail_backend

    def capabilities(self):
        return {
            "provider": "test",
            "model": "test",
            "supportsThinking": False,
            "supportedLevels": ["off"],
            "defaultLevel": "off",
            "maxTokens": {"min": 1, "max": 6000},
        }

    async def generate(self, system_prompt, user_prompt, config=None):
        agent_id = str((config or {}).get("agent_id"))
        self.calls.append(agent_id)
        if self.fail_backend and agent_id == "backend_agent" and self.calls.count(agent_id) == 1:
            raise LLMError("temporary backend failure")
        text = f"{agent_id} output"
        return LLMResponse(text=text, input_tokens=3, output_tokens=4, finish_reason="stop", message_content=text)


def _workflow() -> WorkflowDefinition:
    return WorkflowDefinition(
        id="recoverable",
        name="recoverable",
        steps=[
            StepDefinition("before", agent_id="requirement_agent", task_template="{{requirement}}", output="before", retry_count=0),
            StepDefinition("backend", agent_id="backend_agent", depends_on=["before"], task_template="{{before}}", output="backend", retry_count=0),
        ],
    )


def _make_run(repository: SQLiteRepository, run_id: str, workflow: WorkflowDefinition) -> None:
    repository.create_run(run_id, workflow.id, {"requirement": "build a student system"}, RunStatus.PENDING.value, "now")


@pytest.mark.asyncio
async def test_failed_run_retries_only_failed_node_and_persists_recovery_count():
    repository = SQLiteRepository(":memory:")
    provider = FailBackendOnceProvider()
    workflow = _workflow()
    _make_run(repository, "run_retry", workflow)
    executor = WorkflowExecutor(AgentRegistry.from_directory("agents"), provider, WorkflowEventBus(), repository)

    await executor.start("run_retry", workflow, {"requirement": "build a student system"})
    assert repository.get_run("run_retry")["status"] == RunStatus.FAILED.value

    recovery_count = await executor.retry("run_retry", workflow)
    await asyncio.sleep(0.1)

    run = repository.get_run("run_retry")
    assert recovery_count == 1
    assert run["status"] == RunStatus.SUCCESS.value
    assert run["recovery_count"] == 1
    assert provider.calls == ["requirement_agent", "backend_agent", "backend_agent"]
    assert any(event["type"] == "workflow.recovered" for event in executor.event_bus._history["run_retry"])
    assert run["state"]["checkpoint_thread_id"] == "run_retry:recovery:1"


@pytest.mark.asyncio
async def test_stopped_in_flight_node_resumes_from_stable_checkpoint():
    class BlockingBackendProvider(FailBackendOnceProvider):
        def __init__(self):
            super().__init__(fail_backend=False)
            self.backend_started = asyncio.Event()

        async def generate(self, system_prompt, user_prompt, config=None):
            agent_id = str((config or {}).get("agent_id"))
            if agent_id == "backend_agent" and not self.backend_started.is_set():
                self.backend_started.set()
                await asyncio.Event().wait()
            return await super().generate(system_prompt, user_prompt, config)

    repository = SQLiteRepository(":memory:")
    provider = BlockingBackendProvider()
    workflow = _workflow()
    _make_run(repository, "run_stopped", workflow)
    executor = WorkflowExecutor(AgentRegistry.from_directory("agents"), provider, WorkflowEventBus(), repository)
    task = asyncio.create_task(executor.start("run_stopped", workflow, {"requirement": "build a student system"}))
    await asyncio.wait_for(provider.backend_started.wait(), 5)
    await executor.stop("run_stopped")
    with pytest.raises(asyncio.CancelledError):
        await task
    assert repository.get_run("run_stopped")["status"] == RunStatus.STOPPED.value
    recovery_count = await executor.retry("run_stopped", workflow)
    for _ in range(30):
        if repository.get_run("run_stopped")["status"] == RunStatus.SUCCESS.value:
            break
        await asyncio.sleep(0.05)
    assert recovery_count == 1
    assert repository.get_run("run_stopped")["status"] == RunStatus.SUCCESS.value
    assert provider.calls == ["requirement_agent", "backend_agent"]


@pytest.mark.asyncio
async def test_startup_restores_running_run_from_domain_snapshot():
    repository = SQLiteRepository(":memory:")
    provider = FailBackendOnceProvider(fail_backend=False)
    workflow = _workflow()
    _make_run(repository, "run_startup", workflow)
    repository.update_run("run_startup", status=RunStatus.RUNNING.value)
    executor = WorkflowExecutor(AgentRegistry.from_directory("agents"), provider, WorkflowEventBus(), repository)

    # Simulate a process that crashed after Run creation but before the first
    # checkpoint was written. restore_runs must still replay safely.
    class Workflows:
        def get_workflow_for_run(self, run):
            return workflow

    assert executor.restore_runs(Workflows()) == ["run_startup"]
    await asyncio.sleep(0.2)

    run = repository.get_run("run_startup")
    assert run["status"] == RunStatus.SUCCESS.value
    assert run["recovery_count"] == 1
    assert provider.calls == ["requirement_agent", "backend_agent"]


@pytest.mark.asyncio
async def test_worker_resume_reattaches_approval_without_restarting_graph():
    repository = SQLiteRepository(":memory:")
    provider = FailBackendOnceProvider(fail_backend=False)
    workflow = WorkflowDefinition(
        id="approval-recovery",
        name="approval-recovery",
        steps=[
            StepDefinition("before", agent_id="requirement_agent", task_template="{{requirement}}", output="before"),
            StepDefinition("approve", type=StepType.APPROVAL, depends_on=["before"]),
            StepDefinition("after", agent_id="backend_agent", depends_on=["approve"], task_template="{{before}}"),
        ],
    )
    _make_run(repository, "run-approval-worker", workflow)
    first = WorkflowExecutor(AgentRegistry.from_directory("agents"), provider, WorkflowEventBus(repository), repository)
    await first.start("run-approval-worker", workflow, {"requirement": "approve this"})
    assert repository.get_run("run-approval-worker")["status"] == RunStatus.WAITING_APPROVAL.value

    resumed_bus = WorkflowEventBus(repository)
    resumed = WorkflowExecutor(AgentRegistry.from_directory("agents"), provider, resumed_bus, repository)
    recovery_count = await resumed.resume_persisted("run-approval-worker", workflow)

    run = repository.get_run("run-approval-worker")
    assert run["status"] == RunStatus.WAITING_APPROVAL.value
    assert recovery_count == 0
    assert provider.calls == ["requirement_agent"]
    recovered = [event for event in resumed_bus._history["run-approval-worker"] if event["type"] == "workflow.recovered"]
    assert recovered[-1]["payload"]["paused"] is True
    assert recovered[-1]["payload"]["status"] == RunStatus.WAITING_APPROVAL.value


@pytest.mark.asyncio
async def test_events_survive_event_bus_recreation(tmp_path):
    repository = SQLiteRepository(tmp_path / "runs.db")
    repository.create_run("run_events", "events", {}, RunStatus.PENDING.value, "now")
    first_bus = WorkflowEventBus(repository)
    await first_bus.emit("workflow.started", "run_events", {"status": "RUNNING"})
    await first_bus.emit("workflow.completed", "run_events", {"status": "SUCCESS"})
    repository.update_run("run_events", status=RunStatus.SUCCESS.value)

    second_bus = WorkflowEventBus(repository)
    events = [event async for event in second_bus.stream("run_events")]
    assert [event["type"] for event in events] == ["workflow.started", "workflow.completed"]
    assert events[0]["id"] < events[1]["id"]


@pytest.mark.asyncio
async def test_retry_stream_does_not_close_on_old_terminal_event():
    repository = SQLiteRepository(":memory:")
    repository.create_run("run_retry_stream", "events", {}, RunStatus.FAILED.value, "now")
    first_bus = WorkflowEventBus(repository)
    await first_bus.emit("workflow.failed", "run_retry_stream", {"status": "FAILED"})

    # The retry endpoint changes the durable Run status before the new events
    # are published. The replay stream must therefore remain open.
    repository.update_run("run_retry_stream", status=RunStatus.RUNNING.value)
    second_bus = WorkflowEventBus(repository)
    stream = second_bus.stream("run_retry_stream")
    assert (await anext(stream))["type"] == "workflow.failed"

    waiting_for_recovery = asyncio.create_task(anext(stream))
    await asyncio.sleep(0.01)
    assert not waiting_for_recovery.done()

    await second_bus.emit("workflow.recovered", "run_retry_stream", {"status": "RUNNING"})
    await second_bus.emit("workflow.completed", "run_retry_stream", {"status": "SUCCESS"})
    assert (await waiting_for_recovery)["type"] == "workflow.recovered"
    await stream.aclose()


def test_contract_defect_retry_reopens_architecture_and_invalidates_downstream():
    repository = SQLiteRepository(":memory:")
    workflow = WorkflowDefinition(
        id="contract-reopen",
        name="contract-reopen",
        meta={"delivery_contract": True, "contract_reopen_attempts": 2},
        steps=[
            StepDefinition("requirement", agent_id="requirement_agent", output="requirement_doc"),
            StepDefinition("architecture", agent_id="architect_agent", depends_on=["requirement"], output="architecture_doc"),
            StepDefinition("architecture_approval", type=StepType.APPROVAL, depends_on=["architecture"]),
            StepDefinition("backend", agent_id="backend_agent", depends_on=["architecture_approval"], output="backend_result"),
            StepDefinition("tester", agent_id="tester_agent", depends_on=["backend"], output="test_report"),
        ],
    )
    run_id = "run-contract-reopen"
    repository.create_run(run_id, workflow.id, {"requirement": "学生管理系统"}, RunStatus.FAILED.value, "now")
    results = {
        "requirement": StepStatus.SUCCESS,
        "architecture": StepStatus.SUCCESS,
        "architecture_approval": StepStatus.SUCCESS,
        "backend": StepStatus.SUCCESS,
        "tester": StepStatus.FAILED,
    }
    for step_id, status in results.items():
        repository.upsert_step(run_id, step_id, status=status.value)
    frozen_blueprint = {
        "blueprint_id": "bp_test",
        "version": 1,
        "status": "FROZEN",
        "change_requests": [],
    }
    failure = {
        "failure_id": "failure_delivery-api-contract",
        "code": "delivery-api-contract",
        "category": "contract_defect",
        "owner": "architecture",
        "repair_action": "request_blueprint_change",
        "related_contract": "api_contract",
        "summary": "冻结合同缺少 CRUD API",
        "resolved": False,
    }
    context = {
        "requirement": "学生管理系统",
        "requirement_doc": "已确认",
        "architecture_doc": "old",
        "delivery_contract": {"api_contract": []},
        "delivery_contract_hash": "old-hash",
        "compiled_contract": {"openapi": {}},
        "project_blueprint": frozen_blueprint,
        "artifact_validation": {"status": "failed"},
        "delivery_gate": {"deliverable": False},
        "__artifact_files__": [
            {"name": "pom.xml", "content": "old", "step_id": "backend"},
        ],
        "failure_facts": [failure],
    }
    repository.save_run_snapshot(
        run_id,
        {
            "version": 1,
            "run_id": run_id,
            "context": context,
            "results": {key: value.value for key, value in results.items()},
            "blueprint": frozen_blueprint,
        },
    )
    repository.update_run(
        run_id,
        status=RunStatus.FAILED.value,
        blueprint_json=json.dumps(frozen_blueprint),
        failure_facts_json=json.dumps([failure]),
    )
    executor = WorkflowExecutor(
        AgentRegistry.from_directory("agents"),
        FailBackendOnceProvider(fail_backend=False),
        WorkflowEventBus(repository),
        repository,
    )

    run, state, reset_ids = executor._retry_plan(run_id, workflow)
    assert reset_ids == {"architecture", "architecture_approval", "backend", "tester"}

    payload = executor._prepare_contract_reopen(
        state,
        run,
        reset_ids,
        executor._contract_reopen_failures(run),
    )
    reopened = state.context.snapshot()
    assert payload["attempt"] == 1
    assert "delivery_contract" not in reopened
    assert "compiled_contract" not in reopened
    assert reopened["__artifact_files__"] == []
    assert reopened["contract_reopen_count"] == 1
    assert reopened["failure_facts"][0]["resolved"] is True
    assert state.blueprint is None
    assert repository.get_run(run_id)["blueprint"]["status"] == "CHANGE_REQUESTED"
    repository._connection.close()


def test_retry_plan_uses_failure_fact_owner_when_all_steps_were_successful():
    repository = SQLiteRepository(":memory:")
    workflow = WorkflowDefinition(
        id="fact-retry",
        name="fact-retry",
        steps=[
            StepDefinition("architecture", agent_id="architect_agent"),
            StepDefinition("frontend", agent_id="frontend_agent", depends_on=["architecture"]),
            StepDefinition("tester", agent_id="tester_agent", depends_on=["frontend"]),
        ],
    )
    run_id = "run-fact-retry"
    repository.create_run(run_id, workflow.id, {}, RunStatus.FAILED.value, "now")
    results = {step.id: StepStatus.SUCCESS for step in workflow.steps}
    for step in workflow.steps:
        repository.upsert_step(run_id, step.id, status=StepStatus.SUCCESS.value)
    failure = {
        "failure_id": "failure-browser-crud",
        "code": "browser-crud",
        "category": "integration",
        "owner": "frontend",
        "stage": "integration",
        "repairable": True,
        "retryable": True,
        "repair_action": "dispatch_owner_repair",
        "summary": "浏览器 CRUD 未通过",
        "resolved": False,
    }
    repository.save_run_snapshot(
        run_id,
        {
            "version": 1,
            "run_id": run_id,
            "context": {"failure_facts": [failure]},
            "results": {key: value.value for key, value in results.items()},
        },
    )
    repository.update_run(run_id, failure_facts_json=json.dumps([failure]))
    executor = WorkflowExecutor(
        AgentRegistry.from_directory("agents"),
        FailBackendOnceProvider(fail_backend=False),
        WorkflowEventBus(repository),
        repository,
    )

    _, _, reset_ids = executor._retry_plan(run_id, workflow)

    assert reset_ids == {"frontend", "tester"}


def test_retry_plan_blocks_platform_or_validator_failures():
    repository = SQLiteRepository(":memory:")
    workflow = WorkflowDefinition(
        id="platform-failure",
        name="platform-failure",
        steps=[StepDefinition("tester", agent_id="tester_agent")],
    )
    run_id = "run-platform-failure"
    repository.create_run(run_id, workflow.id, {}, RunStatus.FAILED.value, "now")
    repository.upsert_step(run_id, "tester", status=StepStatus.SUCCESS.value)
    failure = {
        "failure_id": "failure-validator",
        "code": "validator-error",
        "category": "validator_defect",
        "owner": "platform",
        "summary": "验证器测试数据类型错误",
        "resolved": False,
    }
    repository.save_run_snapshot(
        run_id,
        {
            "version": 1,
            "run_id": run_id,
            "context": {"failure_facts": [failure]},
            "results": {"tester": StepStatus.SUCCESS.value},
        },
    )
    repository.update_run(run_id, failure_facts_json=json.dumps([failure]))
    executor = WorkflowExecutor(
        AgentRegistry.from_directory("agents"),
        FailBackendOnceProvider(fail_backend=False),
        WorkflowEventBus(repository),
        repository,
    )

    with pytest.raises(ValueError, match="验证器测试数据类型错误"):
        executor.validate_retry(run_id, workflow)


def test_latest_resolved_failure_fact_supersedes_stale_unresolved_copy():
    unresolved = {
        "failure_id": "failure-api",
        "code": "api-contract",
        "owner": "backend",
        "repairable": True,
        "resolved": False,
    }
    resolved = {**unresolved, "resolved": True}

    latest = WorkflowExecutor._latest_failure_facts(
        {"failure_facts": [unresolved, resolved]}
    )

    assert latest == [resolved]
    reset_ids, blockers = WorkflowExecutor._failure_fact_retry_plan(
        {"failure_facts": [unresolved, resolved]},
        WorkflowDefinition(
            id="resolved-fact",
            name="resolved-fact",
            steps=[StepDefinition("backend", agent_id="backend_agent")],
        ),
    )
    assert reset_ids == set()
    assert blockers == []
