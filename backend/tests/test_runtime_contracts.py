import asyncio

from app.llm.base import LLMProvider, LLMResponse
from app.workflow.budget import TokenBudgetManager
from app.workflow.context import WorkflowContext
from app.workflow.context_packer import ContextPacker
from app.workflow.output_inspector import inspect_response


def test_token_budget_respects_provider_limits_and_context_window():
    manager = TokenBudgetManager(default_context_window=4096, safety_margin=256)

    decision = manager.plan(
        "system",
        "short prompt",
        10000,
        {"maxTokens": {"min": 1, "max": 6000}, "contextWindow": 4096},
    )

    assert decision.effective_max_tokens <= 6000
    assert decision.effective_max_tokens < 10000
    assert decision.input_tokens > 0
    assert decision.warnings


def test_context_packer_keeps_references_and_compacts_large_fields():
    context = WorkflowContext({"requirement": "开发一个复杂平台", "document": "内容" * 3000})
    packet = ContextPacker().pack(
        "需求：{{requirement}}\n文档：{{document}}",
        context,
        120,
        preserve_keys=("requirement",),
    )

    assert packet.text
    assert packet.input_tokens <= 300
    assert packet.truncated_keys == ("document",)
    assert "开发一个复杂平台" in packet.text
    assert "原始内容保留在 Run State" in packet.text


def test_output_inspector_distinguishes_truncated_and_invalid_results():
    truncated = inspect_response(
        LLMResponse("{\"name\":", finish_reason="length"),
        "json",
    )
    invalid = inspect_response(
        LLMResponse("{\"name\":", finish_reason="stop"),
        "json",
    )
    complete = inspect_response(
        LLMResponse("{\"name\": \"ok\"}", finish_reason="stop"),
        "json",
    )

    assert truncated.partial is True
    assert truncated.reason == "output_truncated"
    assert invalid.complete is False
    assert invalid.reason == "invalid_json"
    assert complete.complete is True


class TruncatedThenCompleteProvider(LLMProvider):
    def __init__(self) -> None:
        self.calls = 0
        self.configs: list[dict] = []

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
        self.calls += 1
        self.configs.append(config or {})
        if self.calls == 1:
            return LLMResponse(
                '{"name": "Orbit"',
                output_tokens=4,
                finish_reason="length",
                message_content='{"name": "Orbit"',
            )
        return LLMResponse(
            "}",
            output_tokens=1,
            finish_reason="stop",
            message_content="}",
        )


def test_executor_continues_a_truncated_json_response():
    from app.agents.registry import AgentRegistry
    from app.repositories.sqlite import SQLiteRepository
    from app.workflow.events import WorkflowEventBus
    from app.workflow.executor import WorkflowExecutor
    from app.workflow.models import RunStatus, StepDefinition, WorkflowDefinition

    provider = TruncatedThenCompleteProvider()
    workflow = WorkflowDefinition(
        id="continuation",
        name="continuation",
        steps=[
            StepDefinition(
                "plan",
                agent_id="requirement_agent",
                task_template="{{requirement}}",
                output="plan",
                retry_count=1,
                output_format="json",
            )
        ],
    )
    repository = SQLiteRepository(":memory:")
    repository.create_run("run_continuation", "continuation", {}, RunStatus.PENDING.value, "now")
    executor = WorkflowExecutor(
        AgentRegistry.from_directory("agents"),
        provider,
        WorkflowEventBus(),
        repository,
    )

    asyncio.run(executor.start("run_continuation", workflow, {"requirement": "build plan"}))

    run = repository.get_run("run_continuation")
    assert run["status"] == RunStatus.SUCCESS.value
    assert run["steps"][0]["output"] == '{"name": "Orbit"}'
    assert provider.calls == 2
    assert provider.configs[1]["continuation"] is True
    assert any(event["type"] == "step.continuing" for event in executor.event_bus._history["run_continuation"])


class RuntimeCaptureProvider(LLMProvider):
    def __init__(self) -> None:
        self.configs: list[dict] = []

    def capabilities(self):
        return {
            "provider": "test",
            "model": "test",
            "supportsThinking": False,
            "supportedLevels": ["off"],
            "defaultLevel": "off",
            "maxTokens": {"min": 1, "max": 128000},
        }

    async def generate(self, system_prompt, user_prompt, config=None):
        self.configs.append(config or {})
        return LLMResponse("ok", finish_reason="stop", message_content="ok")


def test_manual_mode_respects_each_agents_custom_token_limit():
    from app.agents.registry import AgentRegistry
    from app.repositories.sqlite import SQLiteRepository
    from app.workflow.events import WorkflowEventBus
    from app.workflow.executor import WorkflowExecutor
    from app.workflow.models import RunStatus, StepDefinition, WorkflowDefinition

    provider = RuntimeCaptureProvider()
    workflow = WorkflowDefinition(
        id="per-agent-runtime",
        name="per-agent-runtime",
        steps=[
            StepDefinition(
                "requirement",
                agent_id="requirement_agent",
                task_template="{{requirement}}",
                output="requirement_doc",
                max_tokens=1200,
            )
        ],
    )
    repository = SQLiteRepository(":memory:")
    repository.create_run("run_per_agent_runtime", "per-agent-runtime", {}, RunStatus.PENDING.value, "now")
    executor = WorkflowExecutor(
        AgentRegistry.from_directory("agents"),
        provider,
        WorkflowEventBus(),
        repository,
    )

    asyncio.run(
        executor.start(
            "run_per_agent_runtime",
            workflow,
            {"requirement": "build a complex platform"},
            {
                "budget_mode": "manual",
                "defaults": {"max_tokens": 6000},
                "steps": {"requirement": {"max_tokens": 4321}},
            },
        )
    )

    assert repository.get_run("run_per_agent_runtime")["status"] == RunStatus.SUCCESS.value
    assert provider.configs[0]["max_tokens"] == 4321


class BudgetPlanProvider(LLMProvider):
    def __init__(self, *, invalid_plan: bool = False, truncated_plan: bool = False) -> None:
        self.invalid_plan = invalid_plan
        self.truncated_plan = truncated_plan
        self.configs: list[dict] = []

    def capabilities(self):
        return {
            "provider": "test",
            "model": "strong-test-model",
            "supportsThinking": True,
            "supportedLevels": ["off", "low", "high", "max"],
            "defaultLevel": "high",
            "maxTokens": {"min": 1, "max": 6000},
        }

    async def generate(self, system_prompt, user_prompt, config=None):
        config = config or {}
        self.configs.append(config)
        if config.get("agent_id") == "token_budget_agent":
            text = "not json" if self.invalid_plan else (
                '{"difficulty":"medium","model_strength":"strong","confidence":0.9,'
                '"agent_budgets":{"reviewer":4300},'
                '"thinking":{"reviewer":"off"},"rationale":["需要前后端"]}'
            )
            return LLMResponse(
                text,
                finish_reason="length" if self.truncated_plan else "stop",
                message_content=text,
                reasoning_content="模型在尾部额外输出了推理" if self.truncated_plan else None,
            )
        return LLMResponse("requirements ready", finish_reason="stop", message_content="requirements ready")


def _budget_plan_workflow():
    from app.workflow.models import StepDefinition, WorkflowDefinition

    return WorkflowDefinition(
        id="budget-plan",
        name="budget-plan",
        steps=[
            StepDefinition(
                "requirement",
                agent_id="requirement_agent",
                task_template="{{requirement}}",
                output="requirement_doc",
                max_tokens=6000,
            ),
            StepDefinition(
                "token_estimator",
                agent_id="token_budget_agent",
                depends_on=["requirement"],
                task_template="{{requirement}} {{requirement_doc}} {{provider_capabilities}} {{workflow_steps}}",
                output="token_budget_plan",
                retry_count=0,
                max_tokens=800,
                thinking="off",
                output_format="json",
                failure_policy="continue",
                runtime_plan=True,
            ),
            StepDefinition(
                "reviewer",
                agent_id="reviewer_agent",
                depends_on=["token_estimator"],
                task_template="{{requirement_doc}}",
                output="review_result",
                max_tokens=6000,
            ),
        ],
    )


def test_runtime_plan_agent_controls_downstream_budget_and_thinking():
    from app.agents.registry import AgentRegistry
    from app.repositories.sqlite import SQLiteRepository
    from app.workflow.events import WorkflowEventBus
    from app.workflow.executor import WorkflowExecutor
    from app.workflow.models import RunStatus

    provider = BudgetPlanProvider()
    repository = SQLiteRepository(":memory:")
    repository.create_run("run_budget_plan", "budget-plan", {}, RunStatus.PENDING.value, "now")
    executor = WorkflowExecutor(AgentRegistry.from_directory("agents"), provider, WorkflowEventBus(), repository)

    asyncio.run(executor.start(
        "run_budget_plan",
        _budget_plan_workflow(),
        {"requirement": "build a shopping site"},
        {
            "budget_mode": "auto",
            "steps": {
                "token_estimator": {"max_tokens": 6000, "retry": 2, "thinking": "high"},
            },
        },
    ))

    planner_config = next(item for item in provider.configs if item.get("agent_id") == "token_budget_agent")
    requirement_config = next(item for item in provider.configs if item.get("agent_id") == "requirement_agent")
    reviewer_config = next(item for item in provider.configs if item.get("agent_id") == "reviewer_agent")
    assert repository.get_run("run_budget_plan")["status"] == RunStatus.SUCCESS.value
    assert [item.get("agent_id") for item in provider.configs] == ["requirement_agent", "token_budget_agent", "reviewer_agent"]
    assert planner_config["max_tokens"] == 800
    assert planner_config["effective_thinking"] == "off"
    assert requirement_config["max_tokens"] == 1800
    assert requirement_config["effective_thinking"] == "low"
    assert reviewer_config["max_tokens"] == 4300
    assert reviewer_config["effective_thinking"] == "off"
    assert any(event["type"] == "workflow.budget_planned" for event in executor.event_bus._history["run_budget_plan"])


def test_optional_runtime_plan_failure_uses_local_fallback_without_blocking_workflow():
    from app.agents.registry import AgentRegistry
    from app.repositories.sqlite import SQLiteRepository
    from app.workflow.events import WorkflowEventBus
    from app.workflow.executor import WorkflowExecutor
    from app.workflow.models import RunStatus

    provider = BudgetPlanProvider(invalid_plan=True)
    repository = SQLiteRepository(":memory:")
    repository.create_run("run_budget_fallback", "budget-plan", {}, RunStatus.PENDING.value, "now")
    executor = WorkflowExecutor(AgentRegistry.from_directory("agents"), provider, WorkflowEventBus(), repository)

    asyncio.run(executor.start("run_budget_fallback", _budget_plan_workflow(), {"requirement": "build a simple page"}, {"budget_mode": "auto"}))

    run = repository.get_run("run_budget_fallback")
    statuses = {step["step_id"]: step["status"] for step in run["steps"]}
    assert run["status"] == RunStatus.SUCCESS.value
    assert statuses == {"requirement": "SUCCESS", "token_estimator": "SUCCESS", "reviewer": "SUCCESS"}
    fallback_event = next(event for event in executor.event_bus._history["run_budget_fallback"] if event["type"] == "workflow.budget_planned")
    assert fallback_event["payload"]["fallback"] is True


def test_complete_runtime_plan_is_accepted_when_provider_only_truncates_tail():
    from app.agents.registry import AgentRegistry
    from app.repositories.sqlite import SQLiteRepository
    from app.workflow.events import WorkflowEventBus
    from app.workflow.executor import WorkflowExecutor
    from app.workflow.models import RunStatus

    provider = BudgetPlanProvider(truncated_plan=True)
    repository = SQLiteRepository(":memory:")
    repository.create_run("run_budget_truncated_tail", "budget-plan", {}, RunStatus.PENDING.value, "now")
    executor = WorkflowExecutor(AgentRegistry.from_directory("agents"), provider, WorkflowEventBus(), repository)

    asyncio.run(executor.start("run_budget_truncated_tail", _budget_plan_workflow(), {"requirement": "build a simple page"}, {"budget_mode": "auto"}))

    run = repository.get_run("run_budget_truncated_tail")
    statuses = {step["step_id"]: step["status"] for step in run["steps"]}
    assert run["status"] == RunStatus.SUCCESS.value
    assert statuses == {"requirement": "SUCCESS", "token_estimator": "SUCCESS", "reviewer": "SUCCESS"}
    assert not any(event["type"] == "workflow.budget_planned" and event["payload"].get("fallback") for event in executor.event_bus._history["run_budget_truncated_tail"])
