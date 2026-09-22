"""Approval dependency regressions using actual LangGraph and offline Providers."""

from __future__ import annotations

import asyncio
import json
from contextlib import AsyncExitStack
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from app.agents.registry import AgentRegistry
from app.llm.base import LLMError, LLMProvider, LLMResponse
from app.repositories.sqlite import SQLiteRepository
from app.workflow.events import WorkflowEventBus
from app.workflow.executor import WorkflowExecutor
from app.workflow.models import RunStatus, StepDefinition, StepStatus, StepType, WorkflowDefinition


class OfflineProvider(LLMProvider):
    def __init__(self, architecture="failure"):
        self.architecture = architecture
        self.calls = []

    async def generate(self, system_prompt, user_prompt, config=None):
        agent_id = (config or {}).get("agent_id")
        self.calls.append(agent_id)
        if agent_id == "architect_agent":
            if self.architecture == "failure":
                raise LLMError("Architecture provider failed", retryable=False)
            if self.architecture == "fallback":
                text = "{invalid architecture JSON"
            else:
                text = json.dumps({"project_type": "static_html", "backend_required": False,
                                   "complexity": "low", "summary": "Pure frontend display page",
                                   "frontend_plan": ["HTML"], "backend_reason": "Explicitly no backend",
                                   "risks": [], "required_capabilities": ["frontend"]})
        else:
            text = "Offline dependency test output."
        return LLMResponse(text=text, input_tokens=10, output_tokens=20,
                           finish_reason="stop", message_content=text)


@pytest_asyncio.fixture(params=["memory", "sqlite"])
async def saver(request, tmp_path):
    async with AsyncExitStack() as stack:
        if request.param == "memory":
            yield InMemorySaver()
        else:
            checkpointer = await stack.enter_async_context(
                AsyncSqliteSaver.from_conn_string(str(tmp_path / "approval-checkpoints.sqlite3")))
            await checkpointer.setup()
            yield checkpointer


@pytest.fixture
def repository():
    result = SQLiteRepository(":memory:")
    try:
        yield result
    finally:
        result._connection.close()


def setup_run(repository, saver, provider, steps, *, meta=None):
    workflow = WorkflowDefinition(id="approval-dependencies", name="approval-dependencies",
                                  steps=steps, meta=meta or {})
    bus = WorkflowEventBus(repository)
    executor = WorkflowExecutor(AgentRegistry.from_directory(Path(__file__).resolve().parents[1] / "agents"),
                                provider, bus, repository, checkpointer=saver)
    repository.create_run("approval-run", workflow.id, {}, RunStatus.PENDING.value, "now")
    return executor, workflow


def architecture(*, failure_policy="fail", runtime_plan=False):
    return StepDefinition("architecture", agent_id="architect_agent", task_template="{{requirement}}",
                          output="architecture_doc", output_format="json", retry_count=0,
                          failure_policy=failure_policy, runtime_plan=runtime_plan)


def approval(*dependencies):
    return StepDefinition("architecture_approval", type=StepType.APPROVAL, depends_on=list(dependencies))


def downstream():
    # Use a non-policy-selected step ID so static architecture cannot hide an
    # erroneously executed approval behind the automatic database skip policy.
    return StepDefinition("downstream_database", agent_id="database_agent", depends_on=["architecture_approval"],
                          task_template="Record the dependency test result after approval.", output="database_result", retry_count=0)


def statuses(repository):
    return {step["step_id"]: step["status"] for step in repository.get_run("approval-run")["steps"]}


def assert_no_approval_or_downstream(repository, provider):
    events = repository.list_events("approval-run")
    assert not any(event["type"] in {"step.waiting_approval", "workflow.waiting_approval"} for event in events)
    assert not any(event["type"] == "step.completed" and
                   event["payload"].get("stepId") == "architecture_approval" for event in events)
    assert not any(event["type"] == "step.started" and
                   event["payload"].get("stepId") == "downstream_database" for event in events)
    assert repository._connection.execute("SELECT COUNT(*) FROM approval_requests").fetchone()[0] == 0
    assert "database_agent" not in provider.calls
    assert statuses(repository)["architecture_approval"] == "SKIPPED"
    assert statuses(repository)["downstream_database"] == "SKIPPED"
    assert repository.get_run("approval-run")["status"] == "FAILED"


async def approve_and_finish(executor, repository, workflow):
    assert repository.get_run("approval-run")["status"] == "WAITING_APPROVAL"
    assert statuses(repository)["architecture_approval"] == "WAITING_APPROVAL"
    await executor.approve("approval-run", "approve")
    # approve() dispatches a background graph resume; wait for executor release
    # instead of fixed sleeps or observing a premature terminal database write.
    async with asyncio.timeout(3):
        while executor.is_active(workflow.id):
            await asyncio.sleep(0.005)
    assert repository.get_run("approval-run")["status"] == "SUCCESS"
    assert statuses(repository)["architecture_approval"] == "SUCCESS"
    assert statuses(repository)["downstream_database"] == "SUCCESS"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_policy", ["fail", "fallback"])
async def test_failed_architecture_never_requests_approval_or_calls_downstream(repository, saver, failure_policy):
    provider = OfflineProvider()
    executor, workflow = setup_run(repository, saver, provider,
                                   [architecture(failure_policy=failure_policy), approval("architecture"), downstream()])
    await executor.start("approval-run", workflow, {"requirement": "学生管理系统，增删改查，使用数据库"})
    assert statuses(repository)["architecture"] == "FAILED"
    assert provider.calls == ["architect_agent"]
    assert_no_approval_or_downstream(repository, provider)
    with pytest.raises(ValueError, match="not waiting for approval"):
        await executor.approve("approval-run", "approve")


@pytest.mark.asyncio
async def test_contract_builder_failure_also_blocks_approval(repository, saver, monkeypatch):
    import app.workflow.executor as executor_module
    from pydantic import BaseModel, ValidationError

    class ContractFields(BaseModel):
        fields: list[str]

    def invalid_contract(*args, **kwargs):
        # Reproduce the failure phase of a map/list contract validation error
        # after a real, valid architecture response, without patching execution.
        return ContractFields(fields={"name": "string"})
    monkeypatch.setattr(executor_module, "build_delivery_contract", invalid_contract)
    provider = OfflineProvider("static")
    executor, workflow = setup_run(repository, saver, provider,
                                   [architecture(failure_policy="fallback"), approval("architecture"), downstream()],
                                   meta={"delivery_contract": True})
    await executor.start("approval-run", workflow, {"requirement": "纯前端静态 HTML 展示页面，无需后端"})
    rows = repository.get_run("approval-run")["steps"]
    failed = next(step for step in rows if step["step_id"] == "architecture")
    assert failed["status"] == "FAILED" and "fields" in failed["error_message"]
    # The pre-freeze repair loop now reports its bounded terminal error while
    # retaining the original Pydantic exception as structured evidence.
    assert any(event["type"] == "step.failed" and event["payload"].get("errorType") == "ArchitectureContractError"
               for event in repository.list_events("approval-run"))
    facts = repository.get_run("approval-run")["failure_facts"]
    assert any(item.get("evidence", {}).get("exception") == ValidationError.__name__ for item in facts)
    assert_no_approval_or_downstream(repository, provider)


@pytest.mark.asyncio
async def test_explicit_continue_failure_is_eligible_for_approval(repository, saver):
    provider = OfflineProvider()
    executor, workflow = setup_run(repository, saver, provider,
                                   [architecture(failure_policy="continue"), approval("architecture"), downstream()])
    await executor.start("approval-run", workflow, {"requirement": "学生管理系统，增删改查，使用数据库"})
    assert statuses(repository)["architecture"] == "FAILED"
    assert "database_agent" not in provider.calls  # approval still waits for a decision
    await approve_and_finish(executor, repository, workflow)
    assert provider.calls == ["architect_agent", "database_agent"]


@pytest.mark.asyncio
async def test_optional_runtime_plan_fallback_success_is_eligible(repository, saver):
    provider = OfflineProvider()
    executor, workflow = setup_run(repository, saver, provider,
                                   [architecture(failure_policy="continue", runtime_plan=True),
                                    approval("architecture"), downstream()])
    await executor.start("approval-run", workflow, {"requirement": "学生管理系统，增删改查，使用数据库"})
    assert statuses(repository)["architecture"] == "SUCCESS"
    assert any(event["type"] == "step.completed" and event["payload"].get("fallback")
               for event in repository.list_events("approval-run"))
    await approve_and_finish(executor, repository, workflow)


@pytest.mark.asyncio
async def test_successful_structured_architecture_fallback_is_eligible(repository, saver):
    provider = OfflineProvider("fallback")
    executor, workflow = setup_run(repository, saver, provider,
                                   [architecture(failure_policy="fallback"), approval("architecture"), downstream()])
    await executor.start("approval-run", workflow, {"requirement": "学生管理系统，增删改查，使用数据库"})
    assert statuses(repository)["architecture"] == "SUCCESS"
    assert any(event["type"] == "workflow.policy_decided" and event["payload"].get("fallback")
               for event in repository.list_events("approval-run"))
    await approve_and_finish(executor, repository, workflow)
    assert "database_agent" in provider.calls


@pytest.mark.asyncio
async def test_actual_adaptive_skipped_dependency_is_eligible_for_approval(repository, saver):
    provider = OfflineProvider("static")
    backend = StepDefinition("backend", agent_id="backend_agent", depends_on=["architecture"],
                             task_template="{{architecture_doc}}", output="backend_result")
    executor, workflow = setup_run(repository, saver, provider,
                                   [architecture(), backend, approval("architecture", "backend"), downstream()])
    await executor.start("approval-run", workflow, {"requirement": "纯前端静态 HTML 展示页面，无需后端"})
    assert statuses(repository)["architecture"] == "SUCCESS"
    assert statuses(repository)["backend"] == "SKIPPED"
    assert "backend_agent" not in provider.calls
    skipped = next(step for step in repository.get_run("approval-run")["steps"] if step["step_id"] == "backend")
    assert "adaptive capability policy" in skipped["error_message"]
    await approve_and_finish(executor, repository, workflow)


@pytest.mark.asyncio
async def test_dependency_failure_skip_is_not_an_adaptive_skip_or_successful_fallback(repository, saver):
    provider = OfflineProvider()
    intermediate = StepDefinition("prerequisite", agent_id="requirement_agent", depends_on=["architecture"],
                                  task_template="{{architecture_doc}}", failure_policy="fallback")
    executor, workflow = setup_run(repository, saver, provider,
                                   [architecture(), intermediate, approval("prerequisite"), downstream()])
    await executor.start("approval-run", workflow, {"requirement": "学生管理系统，增删改查，使用数据库"})
    assert statuses(repository)["architecture"] == "FAILED"
    assert statuses(repository)["prerequisite"] == "SKIPPED"
    assert "requirement_agent" not in provider.calls
    assert_no_approval_or_downstream(repository, provider)


@pytest.mark.asyncio
async def test_continue_sibling_does_not_override_another_failed_required_dependency(repository, saver):
    provider = OfflineProvider()
    optional = StepDefinition("optional_control", agent_id="architect_agent", task_template="Optional control",
                              retry_count=0, failure_policy="continue")
    executor, workflow = setup_run(repository, saver, provider,
                                   [architecture(failure_policy="fallback"), optional,
                                    approval("architecture", "optional_control"), downstream()])
    await executor.start("approval-run", workflow, {"requirement": "学生管理系统，增删改查，使用数据库"})
    assert statuses(repository)["architecture"] == "FAILED"
    assert statuses(repository)["optional_control"] == "FAILED"
    assert_no_approval_or_downstream(repository, provider)


@pytest.mark.parametrize("status", [None, StepStatus.PENDING, StepStatus.RUNNING, StepStatus.WAITING_APPROVAL])
@pytest.mark.parametrize("failure_policy", ["continue", "fallback", "fail"])
@pytest.mark.parametrize("listed_for_skip", [False, True])
def test_dependency_helper_rejects_unfinished_states_despite_continue_or_skip_list(status, failure_policy, listed_for_skip):
    state = SimpleNamespace(results={} if status is None else {"upstream": status},
                            policy_skipped_steps={"upstream"} if listed_for_skip else set())
    steps = {"upstream": StepDefinition("upstream", failure_policy=failure_policy)}
    assert WorkflowExecutor._dependency_satisfied(state, "upstream", steps) is False


@pytest.mark.parametrize("status,failure_policy,listed_for_skip,expected", [
    (StepStatus.SUCCESS, "fail", False, True),
    (StepStatus.SKIPPED, "fail", True, True),
    (StepStatus.SKIPPED, "continue", False, False),
    (StepStatus.SKIPPED, "fallback", False, False),
    (StepStatus.FAILED, "continue", False, True),
    (StepStatus.FAILED, "continue", True, True),
    (StepStatus.FAILED, "fail", True, False),
    (StepStatus.FAILED, "fallback", True, False),
])
def test_dependency_helper_requires_actual_terminal_status_for_each_exception(status, failure_policy, listed_for_skip, expected):
    state = SimpleNamespace(results={"upstream": status},
                            policy_skipped_steps={"upstream"} if listed_for_skip else set())
    steps = {"upstream": StepDefinition("upstream", failure_policy=failure_policy)}
    assert WorkflowExecutor._dependency_satisfied(state, "upstream", steps) is expected


@pytest.mark.asyncio
@pytest.mark.parametrize("database_output", ["database_result", "schema_contract"])
async def test_adaptive_database_skip_supplies_empty_files_to_frontend_template(repository, saver, database_output):
    class PromptRecordingProvider(OfflineProvider):
        def __init__(self):
            super().__init__("static")
            self.frontend_prompts = []
        async def generate(self, system_prompt, user_prompt, config=None):
            if (config or {}).get("agent_id") == "frontend_agent":
                self.frontend_prompts.append(user_prompt)
            return await super().generate(system_prompt, user_prompt, config)
    provider = PromptRecordingProvider()
    variable = f"{database_output}_files"
    database = StepDefinition("database", agent_id="database_agent", depends_on=["architecture"],
                              task_template="Database schema", output=database_output)
    frontend = StepDefinition("frontend", agent_id="frontend_agent", depends_on=["database"],
                              task_template="Database contract files: {{" + variable + "}}",
                              output="frontend_result", retry_count=0)
    executor, workflow = setup_run(repository, saver, provider, [architecture(), database, frontend])
    assert variable not in workflow.inputs
    await executor.start("approval-run", workflow, {"requirement": "纯前端静态 HTML 展示页面，无需后端"})
    run = repository.get_run("approval-run")
    assert run["status"] == "SUCCESS"
    assert statuses(repository)["database"] == "SKIPPED"
    assert statuses(repository)["frontend"] == "SUCCESS"
    assert "database_agent" not in provider.calls
    assert provider.calls == ["architect_agent", "frontend_agent"]
    assert len(provider.frontend_prompts) == 1
    assert "Database contract files: []" in provider.frontend_prompts[0]
    assert "{{" not in provider.frontend_prompts[0]
    assert json.loads(run["state"]["context"][variable]) == []
    assert run["state"]["context"][database_output].startswith("Not required:")
    assert not any(event["type"] == "step.failed" for event in repository.list_events("approval-run"))
    saved = await saver.aget_tuple({"configurable": {"thread_id": "approval-run"}})
    assert saved is not None
    assert json.loads(saved.checkpoint["channel_values"]["context"][variable]) == []
