import asyncio
import json
from pathlib import Path

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from app.workflow.architecture_repair import (
    ArchitectureRepairCoordinator, architecture_patch_issue,
    contract_failure_fact, merge_scoped_architecture_patch,
)
from app.workflow.capability_router import CapabilityRouter
from app.workflow.delivery_contract import build_delivery_contract
from app.code_company.contract_compiler import ContractCompiler
from app.workflow.platform_contracts import build_project_blueprint
from app.workflow.executor import WorkflowExecutor
from app.agents.registry import AgentRegistry
from app.llm.mock import MockProvider
from app.repositories.sqlite import SQLiteRepository
from app.workflow.events import WorkflowEventBus
from app.workflow.models import RunStatus, StepDefinition, StepType, WorkflowDefinition
from app.llm.base import LLMProvider, LLMResponse


REQUIREMENT = (
    "使用 Spring Boot 和 Vue 开发课程与学生管理系统，Student 和 Course 独立实体，"
    "分别提供 /api/students、/api/courses 及各自 /{id} 的 CRUD。"
)


def _entity(name):
    return {"name": name, "fields": {"id": "Long", "name": "String"}}


def _decision(api):
    return {"project_type": "web_app", "backend_required": True, "complexity": "high",
            "delivery_contract": {"entities": [_entity("Student"), _entity("Course")], "api_contract": api}}


def _api(path, entity_id=None):
    return {"path": path, "entity_id": entity_id, "methods": ["GET", "POST", "PUT", "DELETE"],
            "fields": ["id", "name"], "payload": {"name": "A"}}


def _validate(candidate):
    spec = CapabilityRouter().route(REQUIREMENT)
    contract = build_delivery_contract(REQUIREMENT, candidate, requirement_spec=spec.model_dump(mode="json"))
    blueprint = build_project_blueprint(REQUIREMENT, spec.model_dump(mode="json"), candidate, contract)
    compiled = ContractCompiler().compile(blueprint)
    if {row.get("entity_id") for row in contract["api_contract"]} != {"Student", "Course"}:
        raise ValueError("API 合同缺少实体 Course 的 entity_id")
    return {"contract": contract, "compiled": compiled}


def test_architecture_patch_preserves_unrelated_api_and_entity_rows():
    original = _decision([_api("/api/students"), _api("/api/courses", "Course")])
    proposed = {"api_contract": [_api("/api/students", "Student"), _api("/api/courses", "Wrong")],
                "entities": [_entity("Wrong")]}
    fixed = merge_scoped_architecture_patch(original, proposed, ["api_contract"],
                                            error="API 合同 /api/students 缺少明确 entity_id")
    assert fixed["delivery_contract"]["api_contract"][0]["entity_id"] == "Student"
    assert fixed["delivery_contract"]["api_contract"][1]["entity_id"] == "Course"
    assert fixed["delivery_contract"]["entities"] == original["delivery_contract"]["entities"]


def test_entity_link_patch_requires_explicit_existing_entity_without_url_guessing():
    candidate = _decision([_api("/api/students")])
    candidate["delivery_contract"]["entities"] = [_entity("Student")]
    fact = contract_failure_fact(ValueError("API 合同缺少实体 student 的 CRUD 路由或 entity_id"), attempt=0)
    assert fact["summary"] == "API 合同缺少明确的实体关联 entity_id。"
    assert "entity_id" in architecture_patch_issue(candidate, {"api_contract": [_api("/api/students")]}, fact)
    assert "已有实体" in architecture_patch_issue(candidate, {"api_contract": [_api("/api/students", "Course")]}, fact)
    assert architecture_patch_issue(candidate, {"api_contract": [_api("/api/students", "Student")]}, fact) is None
    assert candidate["delivery_contract"]["api_contract"][0]["entity_id"] is None


@pytest.mark.asyncio
async def test_single_entity_contract_repair_rejects_missing_link_then_reaches_approval():
    requirement = "设计一个学生管理系统,前端用vue,后端用springboot.要有增删改查的功能"

    class ScriptedProvider(LLMProvider):
        def __init__(self):
            self.prompts = []

        async def generate(self, system_prompt, user_prompt, config=None):
            self.prompts.append((system_prompt, user_prompt, config or {}))
            if len(self.prompts) == 1:
                response = {
                    "project_type": "web_app", "backend_required": True, "complexity": "low",
                    "summary": "学生管理 CRUD", "frontend_plan": ["Vue 列表和表单"],
                    "backend_reason": "需要 Spring Boot API", "risks": [],
                    "delivery_contract": {
                        "entities": [_entity("Student")],
                        "api_contract": [_api("/api/students")],
                    },
                }
            elif len(self.prompts) == 2:
                response = {"api_contract": [_api("/api/students")]}
            else:
                response = {"api_contract": [_api("/api/students", "Student")]}
            body = json.dumps(response, ensure_ascii=False)
            return LLMResponse(text=body, input_tokens=20, output_tokens=30,
                               finish_reason="stop", message_content=body)

    provider = ScriptedProvider()
    repository = SQLiteRepository(":memory:")
    try:
        registry = AgentRegistry.from_directory(Path(__file__).resolve().parents[1] / "agents")
        executor = WorkflowExecutor(registry, provider, WorkflowEventBus(repository), repository,
                                    checkpointer=InMemorySaver())
        workflow = WorkflowDefinition(id="single-entity-repair", name="single-entity-repair", meta={"delivery_contract": True},
                                      steps=[StepDefinition("architecture", agent_id="architect_agent",
                                                            task_template="{{requirement}}", output="architecture_doc",
                                                            output_format="json", retry_count=0),
                                             StepDefinition("architecture_approval", type=StepType.APPROVAL,
                                                            depends_on=["architecture"])])
        repository.create_run("single-entity-repair", workflow.id, {}, RunStatus.PENDING.value, "now")
        await executor.start("single-entity-repair", workflow, {"requirement": requirement})
        events = repository.list_events("single-entity-repair")
        assert repository.get_run("single-entity-repair")["status"] == "WAITING_APPROVAL"
        assert len(provider.prompts) == 3
        assert "entity_id" in provider.prompts[0][0]
        assert "api_contract[0] 仍缺少 entity_id" in provider.prompts[2][1]
        assert any(event["type"] == "architecture.repair_rejected" for event in events)
        assert any(event["type"] == "architecture.target_gate_completed" for event in events)
        assert not any(event["type"] == "architecture.repair_circuit_open" for event in events)
        assert repository.get_run("single-entity-repair")["blueprint"]["status"] == "DRAFT"
    finally:
        repository._connection.close()


@pytest.mark.asyncio
async def test_architecture_backedge_repairs_only_api_contract_and_is_checkpoint_idempotent():
    initial = _decision([_api("/api/items"), _api("/api/courses")])
    saver = InMemorySaver()
    coordinator = ArchitectureRepairCoordinator(saver)
    calls = []
    events = []

    async def repair(candidate, fact, attempt):
        calls.append((fact["code"], attempt))
        return {"project_type": "static_html", "backend_required": False,
                "api_contract": [_api("/api/students", "Student"), _api("/api/courses", "Course")]}

    async def emit(event, payload):
        events.append(event)

    result = await coordinator.coordinate(run_id="arch-backedge", candidate=initial, max_attempts=2,
                                          validate=_validate, repair=repair, emit=emit)
    assert result["passed"] is True
    assert result["attempt"] == 1
    assert calls == [("ARCH_API_ENTITY_ID_REQUIRED", 1)]
    assert initial["delivery_contract"]["api_contract"][0]["path"] == "/api/items"
    assert result["candidate"]["project_type"] == "web_app"
    assert result["candidate"]["backend_required"] is True
    assert "architecture.contract_failed" in events
    assert "architecture.target_gate_completed" in events
    resumed = await coordinator.coordinate(run_id="arch-backedge", candidate=initial, max_attempts=2,
                                           validate=_validate, repair=repair, emit=emit)
    assert resumed["passed"] is True and len(calls) == 1


@pytest.mark.asyncio
async def test_ambiguous_architecture_contract_stops_after_bounded_no_progress():
    initial = _decision([_api("/api/items"), _api("/api/courses")])
    calls = []

    async def repair(candidate, fact, attempt):
        calls.append(attempt)
        return {"api_contract": candidate["delivery_contract"]["api_contract"]}

    result = await ArchitectureRepairCoordinator().coordinate(
        run_id="arch-ambiguous", candidate=initial, max_attempts=3,
        validate=_validate, repair=repair,
    )
    assert result["passed"] is False
    assert result["failure"]["category"] == "architecture_contract"
    assert result["failure"]["owner"] == "architecture"
    assert result["no_progress"] == 2
    assert calls == [1, 2]


@pytest.mark.asyncio
async def test_architecture_repair_cancellation_resumes_from_checkpoint_without_freeze():
    saver = InMemorySaver()
    coordinator = ArchitectureRepairCoordinator(saver)
    started = asyncio.Event()
    calls = []

    async def interrupted_repair(candidate, fact, attempt):
        calls.append(attempt)
        started.set()
        await asyncio.Event().wait()
        return {}

    task = asyncio.create_task(coordinator.coordinate(
        run_id="arch-cancel", candidate=_decision([_api("/api/items")]), max_attempts=2,
        validate=_validate, repair=interrupted_repair,
    ))
    await asyncio.wait_for(started.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    async def resumed_repair(candidate, fact, attempt):
        calls.append(attempt)
        return {"api_contract": [_api("/api/students", "Student"), _api("/api/courses", "Course")]}

    result = await coordinator.coordinate(
        run_id="arch-cancel", candidate=_decision([_api("/api/items")]), max_attempts=2,
        validate=_validate, repair=resumed_repair,
    )
    assert result["passed"] is True
    assert calls == [1, 1, 2]
    assert result["attempt"] == 2


@pytest.mark.parametrize("names, paths", [
    (("Student", "Course"), ("/api/students", "/api/courses")),
    (("Product", "Order"), ("/api/products", "/api/orders")),
])
def test_multi_entity_user_named_routes_compile_before_approval(names, paths):
    requirement = (
        f"使用 Spring Boot 和 Vue 开发 {names[0]} 与 {names[1]} 管理系统。"
        f"两个独立实体分别提供 {paths[0]}、{paths[1]} 及各自 /{{id}} 的 CRUD。"
    )
    spec = CapabilityRouter().route(requirement)
    decision = _decision([_api(paths[0]), _api(paths[1])])
    decision["delivery_contract"]["entities"] = [_entity(name) for name in names]
    repository = SQLiteRepository(":memory:")
    executor = WorkflowExecutor(AgentRegistry.from_directory("agents"), MockProvider(), WorkflowEventBus(repository), repository)
    result = executor._validate_architecture_candidate(decision, spec)
    assert {row["entity_id"] for row in result["contract"]["api_contract"]} == set(names)
    assert result["blueprint"]["status"] == "DRAFT"
    assert set(paths) <= set(result["compiled_contract"]["openapi"]["paths"])
