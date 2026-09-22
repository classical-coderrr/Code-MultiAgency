import asyncio
import json

import pytest

from app.agents.registry import AgentRegistry
from app.llm.mock import MockProvider
from app.llm.base import LLMProvider, LLMResponse
from app.repositories.sqlite import SQLiteRepository
from app.workflow.events import WorkflowEventBus
from app.workflow.executor import WorkflowExecutor
from app.workflow.models import StepDefinition, StepType, WorkflowDefinition, RunStatus


class StaticHtmlProvider(LLMProvider):
    def __init__(self, allow_backend=False):
        self.calls = []
        self.allow_backend = allow_backend

    def capabilities(self):
        return {
            "provider": "test",
            "model": "test",
            "supportsThinking": True,
            "supportedLevels": ["off", "low", "high", "max"],
            "defaultLevel": "high",
            "maxTokens": {"min": 1, "max": 128000},
        }

    async def generate(self, system_prompt, user_prompt, config=None):
        config = config or {}
        agent_id = config.get("agent_id")
        self.calls.append((agent_id, config.copy()))
        if agent_id == "backend_agent" and not self.allow_backend:
            raise AssertionError("Backend should be skipped for a static HTML project")
        outputs = {
            "requirement_agent": "A simple advertisement page is required.",
            "architect_agent": json.dumps({
                "project_type": "static_html",
                "backend_required": False,
                "complexity": "low",
                "summary": "A directly opened static advertisement page.",
                "frontend_plan": ["Semantic HTML", "Responsive CSS", "Small JavaScript interactions"],
                "backend_reason": "No API, database, login, or payment is required.",
                "risks": ["Verify mobile layout"],
            }),
            "backend_agent": "CRUD API and student data service implementation.",
            "frontend_agent": "Use semantic HTML, responsive CSS, and a small JavaScript interaction.",
            "tester_agent": "Check layout, links, and responsive behavior.",
            "reviewer_agent": "The static HTML plan is ready.",
        }
        text = outputs[agent_id]
        return LLMResponse(text=text, input_tokens=10, output_tokens=20, finish_reason="stop", message_content=text)


@pytest.mark.asyncio
async def test_full_workflow_pauses_and_continues():
    workflow = WorkflowDefinition(id="e2e", name="e2e", steps=[
        StepDefinition("requirement", agent_id="requirement_agent", task_template="{{requirement}}", output="requirement_doc"),
        StepDefinition("approval", type=StepType.APPROVAL, depends_on=["requirement"]),
        StepDefinition("backend", agent_id="backend_agent", depends_on=["approval"], task_template="{{requirement_doc}}", output="backend_result"),
        StepDefinition("frontend", agent_id="frontend_agent", depends_on=["approval"], task_template="{{requirement_doc}}", output="frontend_result"),
        StepDefinition("reviewer", agent_id="reviewer_agent", depends_on=["backend", "frontend"], task_template="{{backend_result}} {{frontend_result}}", output="final_report"),
    ])
    registry = AgentRegistry.from_directory("agents")
    repo = SQLiteRepository(":memory:")
    bus = WorkflowEventBus()
    executor = WorkflowExecutor(registry, MockProvider(), bus, repo)
    repo.create_run("run_e2e", "e2e", {"requirement": "build app"}, RunStatus.PENDING.value, "now")

    await executor.start("run_e2e", workflow, {"requirement": "build app"})
    assert repo.get_run("run_e2e")["status"] == RunStatus.WAITING_APPROVAL.value
    await executor.approve("run_e2e", "approve")
    await asyncio.sleep(1.2)
    assert repo.get_run("run_e2e")["status"] == RunStatus.SUCCESS.value
    assert repo.get_run("run_e2e")["final_report"]


@pytest.mark.asyncio
async def test_waiting_approval_can_be_restored_after_backend_restart():
    workflow = WorkflowDefinition(id="recover", name="recover", steps=[
        StepDefinition("requirement", agent_id="requirement_agent", task_template="{{requirement}}", output="requirement_doc"),
        StepDefinition("approval", type=StepType.APPROVAL, depends_on=["requirement"]),
        StepDefinition("backend", agent_id="backend_agent", depends_on=["approval"], task_template="{{requirement_doc}}", output="backend_result"),
        StepDefinition("frontend", agent_id="frontend_agent", depends_on=["approval"], task_template="{{requirement_doc}}", output="frontend_result"),
    ])
    repo = SQLiteRepository(":memory:")
    repo.create_run("run_recover", "recover", {"requirement": "build app"}, RunStatus.PENDING.value, "now")

    original_executor = WorkflowExecutor(AgentRegistry.from_directory("agents"), MockProvider(), WorkflowEventBus(), repo)
    await original_executor.start("run_recover", workflow, {"requirement": "build app"})
    assert repo.get_run("run_recover")["status"] == RunStatus.WAITING_APPROVAL.value

    restarted_executor = WorkflowExecutor(AgentRegistry.from_directory("agents"), MockProvider(), WorkflowEventBus(), repo)
    assert restarted_executor.restore_waiting_run(repo.get_run("run_recover"), workflow)
    await restarted_executor.approve("run_recover", "approve")
    await asyncio.sleep(0.8)

    run = repo.get_run("run_recover")
    assert run["status"] == RunStatus.SUCCESS.value
    assert {step["step_id"] for step in run["steps"]} == {"requirement", "approval", "backend", "frontend"}


@pytest.mark.asyncio
async def test_static_html_architecture_skips_backend_and_continues_downstream():
    workflow = WorkflowDefinition(id="adaptive", name="adaptive", steps=[
        StepDefinition("requirement", agent_id="requirement_agent", task_template="{{requirement}}", output="requirement_doc"),
        StepDefinition("architecture", agent_id="architect_agent", depends_on=["requirement"], task_template="{{requirement_doc}}", output="architecture_doc"),
        StepDefinition("approval", type=StepType.APPROVAL, depends_on=["architecture"]),
        StepDefinition("backend", agent_id="backend_agent", depends_on=["approval"], task_template="{{architecture_doc}}", output="backend_result"),
        StepDefinition("frontend", agent_id="frontend_agent", depends_on=["approval"], task_template="{{architecture_doc}}", output="frontend_result"),
        StepDefinition("tester", agent_id="tester_agent", depends_on=["backend", "frontend"], task_template="{{backend_result}} {{frontend_result}}", output="test_report"),
        StepDefinition("reviewer", agent_id="reviewer_agent", depends_on=["tester"], task_template="{{architecture_doc}} {{backend_result}} {{frontend_result}} {{test_report}}", output="final_report"),
    ])
    provider = StaticHtmlProvider()
    repo = SQLiteRepository(":memory:")
    executor = WorkflowExecutor(AgentRegistry.from_directory("agents"), provider, WorkflowEventBus(), repo)
    repo.create_run("run_adaptive", "adaptive", {"requirement": "build a simple ad HTML page"}, RunStatus.PENDING.value, "now")

    await executor.start("run_adaptive", workflow, {"requirement": "build a simple ad HTML page"}, {"budget_mode": "auto", "defaults": {"max_tokens": 9000}})
    assert repo.get_run("run_adaptive")["status"] == RunStatus.WAITING_APPROVAL.value

    await executor.approve("run_adaptive", "approve")
    await asyncio.sleep(0.05)

    run = repo.get_run("run_adaptive")
    statuses = {step["step_id"]: step["status"] for step in run["steps"]}
    assert run["status"] == RunStatus.SUCCESS.value
    assert statuses["backend"] == "SKIPPED"
    assert statuses["frontend"] == "SUCCESS"
    assert statuses["tester"] == "SUCCESS"
    assert statuses["reviewer"] == "SUCCESS"
    assert not any(agent_id == "backend_agent" for agent_id, _ in provider.calls)
    frontend_config = next(config for agent_id, config in provider.calls if agent_id == "frontend_agent")
    assert frontend_config["max_tokens"] == 3200
    assert any(event["type"] == "workflow.policy_decided" for event in executor.event_bus._history["run_adaptive"])


@pytest.mark.asyncio
async def test_crud_request_overrides_misclassified_static_html_route():
    workflow = WorkflowDefinition(id="crud_route", name="crud_route", steps=[
        StepDefinition("requirement", agent_id="requirement_agent", task_template="{{requirement}}", output="requirement_doc"),
        StepDefinition("architecture", agent_id="architect_agent", depends_on=["requirement"], task_template="{{requirement_doc}}", output="architecture_doc"),
        StepDefinition("approval", type=StepType.APPROVAL, depends_on=["architecture"]),
        StepDefinition("backend", agent_id="backend_agent", depends_on=["approval"], task_template="{{architecture_doc}}", output="backend_result"),
        StepDefinition("frontend", agent_id="frontend_agent", depends_on=["approval"], task_template="{{architecture_doc}}", output="frontend_result"),
    ])
    provider = StaticHtmlProvider(allow_backend=True)
    repo = SQLiteRepository(":memory:")
    executor = WorkflowExecutor(AgentRegistry.from_directory("agents"), provider, WorkflowEventBus(), repo)
    requirement = "生成一个学生管理系统，要求有 curd"
    repo.create_run("run_crud_route", "crud_route", {"requirement": requirement}, RunStatus.PENDING.value, "now")

    await executor.start("run_crud_route", workflow, {"requirement": requirement})
    await executor.approve("run_crud_route", "approve")
    await asyncio.sleep(0.05)

    run = repo.get_run("run_crud_route")
    statuses = {step["step_id"]: step["status"] for step in run["steps"]}
    assert run["status"] == RunStatus.SUCCESS.value
    assert statuses["backend"] == "SUCCESS"
    assert statuses["frontend"] == "SUCCESS"
    assert any(agent_id == "backend_agent" for agent_id, _ in provider.calls)
    policy_event = next(event for event in executor.event_bus._history["run_crud_route"] if event["type"] == "workflow.policy_decided")
    assert policy_event["payload"]["decision"]["routing_guard"]["correction_applied"] is True


@pytest.mark.asyncio
async def test_delivery_workflow_pauses_for_ambiguous_scope_and_resumes_from_checkpoint():
    workflow = WorkflowDefinition(id="clarification", name="clarification", meta={"delivery_contract": True}, steps=[
        StepDefinition("requirement", agent_id="requirement_agent", task_template="{{requirement}}", output="requirement_doc"),
        StepDefinition("architecture", agent_id="architect_agent", depends_on=["requirement"], task_template="{{requirement_doc}}", output="architecture_doc"),
        StepDefinition("approval", type=StepType.APPROVAL, depends_on=["architecture"]),
    ])
    provider = StaticHtmlProvider(allow_backend=True)
    repo = SQLiteRepository(":memory:")
    executor = WorkflowExecutor(AgentRegistry.from_directory("agents"), provider, WorkflowEventBus(), repo)
    requirement = "开发一个学生管理系统，需要增删改查"
    repo.create_run("run_clarification", "clarification", {"requirement": requirement}, RunStatus.PENDING.value, "now")

    await executor.start("run_clarification", workflow, {"requirement": requirement})
    waiting = repo.get_run("run_clarification")
    assert waiting["status"] == RunStatus.WAITING_CLARIFICATION.value
    assert waiting["clarification"]["status"] == "OPEN"
    assert not any(agent_id == "architect_agent" for agent_id, _ in provider.calls)

    with pytest.raises(ValueError, match="已更新"):
        await executor.clarify("run_clarification", {"option": "full_stack_h2", "request_id": "stale"})
    await executor.clarify("run_clarification", {
        "option": "full_stack_h2",
        "request_id": waiting["clarification"]["request_id"],
    })
    await asyncio.sleep(0.1)
    resumed = repo.get_run("run_clarification")
    assert resumed["status"] == RunStatus.WAITING_APPROVAL.value
    assert resumed["clarification"]["status"] == "ANSWERED"
    assert resumed["blueprint"]["database"]["mode"] == "h2"
