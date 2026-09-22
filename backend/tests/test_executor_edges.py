import asyncio

import pytest

from app.agents.registry import AgentRegistry
from app.llm.base import LLMError, LLMProvider, LLMResponse, LLMTimeoutError
from app.repositories.sqlite import SQLiteRepository
from app.workflow.events import WorkflowEventBus
from app.workflow.executor import WorkflowExecutor, _can_use_structured_fallback
from app.workflow.models import RunStatus, StepDefinition, WorkflowDefinition


class RetryOnceProvider(LLMProvider):
    def __init__(self):
        self.calls = 0

    async def generate(self, system_prompt, user_prompt, config=None):
        self.calls += 1
        if self.calls == 1:
            raise LLMError("temporary 503", retryable=True)
        return LLMResponse("recovered output")


class SlowProvider(LLMProvider):
    async def generate(self, system_prompt, user_prompt, config=None):
        await asyncio.sleep(0.05)
        return LLMResponse("too late")


class TimeoutThenSuccessProvider(LLMProvider):
    def __init__(self):
        self.calls = 0
        self.configs = []

    def capabilities(self):
        return {
            "provider": "test",
            "model": "test",
            "supportsThinking": True,
            "supportedLevels": ["off", "low", "high", "max"],
            "defaultLevel": "high",
            "maxTokens": {"min": 1, "max": 6000},
        }

    async def generate(self, system_prompt, user_prompt, config=None):
        self.calls += 1
        self.configs.append(config or {})
        if self.calls == 1:
            raise LLMTimeoutError("first attempt timed out")
        return LLMResponse("recovered after lowering reasoning effort")


class EmptyThenSuccessProvider(LLMProvider):
    def __init__(self):
        self.calls = 0
        self.configs = []

    def capabilities(self):
        return {
            "provider": "test",
            "model": "test",
            "supportsThinking": True,
            "supportedLevels": ["off", "low", "high", "max"],
            "defaultLevel": "high",
            "maxTokens": {"min": 1, "max": 6000},
        }

    async def generate(self, system_prompt, user_prompt, config=None):
        self.calls += 1
        self.configs.append(config or {})
        if self.calls == 1:
            return LLMResponse(
                "",
                finish_reason="length",
                message_content="",
                reasoning_content="思考内容已返回，但没有最终答案。",
                usage={"prompt_tokens": 12, "completion_tokens": 6000},
            )
        return LLMResponse(
            "recovered after empty output",
            finish_reason="stop",
            message_content="recovered after empty output",
            usage={"prompt_tokens": 12, "completion_tokens": 24},
        )


class UncontrollableReasoningProvider(LLMProvider):
    def __init__(self):
        self.calls = 0

    async def generate(self, system_prompt, user_prompt, config=None):
        self.calls += 1
        return LLMResponse(
            "",
            output_tokens=6000,
            finish_reason="length",
            message_content="",
            reasoning_content="reasoning only",
            usage={
                "prompt_tokens": 10,
                "completion_tokens": 6000,
                "completion_tokens_details": {"reasoning_tokens": 6000},
            },
        )


class ReasoningOnlyArchitectureProvider(LLMProvider):
    def __init__(self, *, recover: bool) -> None:
        self.recover = recover
        self.calls = 0
        self.configs = []

    def capabilities(self):
        return {
            "provider": "test",
            "model": "structured-reasoning-model",
            "supportsThinking": True,
            "supportedLevels": ["off", "low", "high", "max"],
            "defaultLevel": "high",
            "maxTokens": {"min": 1, "max": 6000},
        }

    async def generate(self, system_prompt, user_prompt, config=None):
        self.calls += 1
        self.configs.append(config or {})
        if self.recover and self.calls == 2:
            text = (
                '{"project_type":"web_app","backend_required":true,"complexity":"low",'
                '"summary":"保留前后端","frontend_plan":["页面与交互"],'
                '"backend_reason":"需要服务端数据边界","risks":[]}'
            )
            return LLMResponse(text, finish_reason="stop", message_content=text)
        return LLMResponse(
            "",
            output_tokens=2000,
            finish_reason="length",
            message_content="",
            reasoning_content="模型只返回了推理内容",
            usage={"prompt_tokens": 100, "completion_tokens": 2000},
        )


class CancelThenSuccessProvider(LLMProvider):
    def __init__(self):
        self.calls = 0
        self.cleanup_done = asyncio.Event()
        self.retry_started_before_cleanup = False

    async def generate(self, system_prompt, user_prompt, config=None):
        self.calls += 1
        if self.calls == 1:
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                await asyncio.sleep(0.02)
                self.cleanup_done.set()
                raise
        if not self.cleanup_done.is_set():
            self.retry_started_before_cleanup = True
        return LLMResponse("recovered after cancellation cleanup")


class CancellableProvider(LLMProvider):
    def __init__(self):
        self.cancelled = asyncio.Event()

    async def generate(self, system_prompt, user_prompt, config=None):
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        return LLMResponse("should not complete")


def make_executor(provider):
    return WorkflowExecutor(
        AgentRegistry.from_directory("agents"), provider, WorkflowEventBus(), SQLiteRepository(":memory:")
    )


def make_run(executor, run_id, workflow):
    executor.repository.create_run(run_id, workflow.id, {}, RunStatus.PENDING.value, "now")


def test_entity_fields_shape_error_is_eligible_for_structured_fallback():
    response = LLMResponse(
        '{"delivery_contract":{"entities":[{"name":"Book","fields":42}]}}',
        finish_reason="stop",
    )

    assert _can_use_structured_fallback(
        response,
        "实体合同 Book.fields 必须是字段名到类型的映射；实际收到 int",
    )


@pytest.mark.asyncio
async def test_llm_retry_and_retry_count_are_recorded():
    workflow = WorkflowDefinition(id="retry", name="retry", steps=[StepDefinition("a", agent_id="requirement_agent", task_template="hi", output="doc", retry_count=1)])
    executor = make_executor(RetryOnceProvider())
    make_run(executor, "run_retry", workflow)
    await executor.start("run_retry", workflow, {})
    run = executor.repository.get_run("run_retry")
    assert run["status"] == RunStatus.SUCCESS.value
    assert run["steps"][0]["retry_count"] == 1


@pytest.mark.asyncio
async def test_first_retry_lowers_reasoning_effort_and_caps_output():
    provider = TimeoutThenSuccessProvider()
    workflow = WorkflowDefinition(id="fallback", name="fallback", steps=[StepDefinition("a", agent_id="requirement_agent", task_template="hi", output="doc", retry_count=1)])
    executor = make_executor(provider)
    make_run(executor, "run_fallback", workflow)

    await executor.start("run_fallback", workflow, {})

    assert provider.calls == 2
    assert provider.configs[0]["reasoning_effort"] == "high"
    assert provider.configs[1]["reasoning_effort"] == "low"
    assert provider.configs[1]["max_tokens"] == 6000
    retry_events = [event for event in executor.event_bus._history["run_fallback"] if event["type"] == "step.retrying"]
    assert retry_events[0]["payload"]["reasoningEffort"] == "low"


@pytest.mark.asyncio
async def test_empty_output_is_retryable_and_records_provider_response():
    provider = EmptyThenSuccessProvider()
    workflow = WorkflowDefinition(id="empty-output", name="empty-output", steps=[StepDefinition("a", agent_id="requirement_agent", task_template="hi", output="doc", retry_count=1)])
    executor = make_executor(provider)
    make_run(executor, "run_empty_output", workflow)

    await executor.start("run_empty_output", workflow, {})

    run = executor.repository.get_run("run_empty_output")
    step = run["steps"][0]
    assert run["status"] == RunStatus.SUCCESS.value
    assert provider.calls == 2
    assert provider.configs[0]["reasoning_effort"] == "high"
    assert provider.configs[1]["reasoning_effort"] == "low"
    assert step["retry_count"] == 1
    assert len(step["provider_attempts"]) == 2
    assert step["provider_attempts"][0]["finish_reason"] == "length"
    assert step["provider_attempts"][0]["message_content"] == ""
    assert step["provider_attempts"][0]["reasoning_content"]
    assert step["provider"]["finish_reason"] == "stop"


@pytest.mark.asyncio
async def test_reasoning_only_response_stops_when_retry_cannot_change_thinking():
    provider = UncontrollableReasoningProvider()
    workflow = WorkflowDefinition(
        id="reasoning-saturation",
        name="reasoning-saturation",
        steps=[StepDefinition("a", agent_id="requirement_agent", task_template="hi", output="doc", retry_count=2)],
    )
    executor = make_executor(provider)
    make_run(executor, "run_reasoning_saturation", workflow)

    await executor.start("run_reasoning_saturation", workflow, {})

    run = executor.repository.get_run("run_reasoning_saturation")
    assert provider.calls == 1
    assert run["status"] == RunStatus.FAILED.value
    assert "已停止无效重试" in run["steps"][0]["error_message"]


@pytest.mark.asyncio
async def test_structured_json_gets_one_automatic_thinking_recovery_even_when_retry_is_zero():
    provider = ReasoningOnlyArchitectureProvider(recover=True)
    workflow = WorkflowDefinition(
        id="architecture-recovery",
        name="architecture-recovery",
        steps=[
            StepDefinition(
                "architecture",
                agent_id="architect_agent",
                task_template="{{requirement}}",
                output="architecture_doc",
                output_format="json",
                retry_count=0,
                failure_policy="fallback",
            )
        ],
    )
    executor = make_executor(provider)
    make_run(executor, "run_architecture_recovery", workflow)

    await executor.start("run_architecture_recovery", workflow, {"requirement": "开发一个学生管理系统，支持增删改查"})

    run = executor.repository.get_run("run_architecture_recovery")
    assert run["status"] == RunStatus.SUCCESS.value
    assert provider.calls == 2
    assert provider.configs[0]["effective_thinking"] == "low"
    assert provider.configs[1]["effective_thinking"] == "off"
    assert run["steps"][0]["retry_count"] == 1
    assert any(
        event["type"] == "step.retrying" and event["payload"].get("automaticRecovery")
        for event in executor.event_bus._history["run_architecture_recovery"]
    )


@pytest.mark.asyncio
async def test_architecture_uses_conservative_local_fallback_after_reasoning_only_recovery_fails():
    provider = ReasoningOnlyArchitectureProvider(recover=False)
    workflow = WorkflowDefinition(
        id="architecture-local-fallback",
        name="architecture-local-fallback",
        steps=[
            StepDefinition(
                "architecture",
                agent_id="architect_agent",
                task_template="{{requirement}}",
                output="architecture_doc",
                output_format="json",
                retry_count=0,
                failure_policy="fallback",
            )
        ],
    )
    executor = make_executor(provider)
    make_run(executor, "run_architecture_local_fallback", workflow)

    await executor.start("run_architecture_local_fallback", workflow, {"requirement": "开发一个学生管理系统，支持增删改查"})

    run = executor.repository.get_run("run_architecture_local_fallback")
    assert run["status"] == RunStatus.SUCCESS.value
    assert run["steps"][0]["status"] == RunStatus.SUCCESS.value
    assert '"backend_required": true' in run["steps"][0]["output"]
    assert any(
        event["type"] == "workflow.policy_decided" and event["payload"].get("fallback")
        for event in executor.event_bus._history["run_architecture_local_fallback"]
    )


@pytest.mark.asyncio
async def test_retry_waits_for_cancelled_provider_cleanup():
    provider = CancelThenSuccessProvider()
    workflow = WorkflowDefinition(id="cancel-retry", name="cancel-retry", steps=[StepDefinition("a", agent_id="requirement_agent", task_template="hi", timeout_seconds=0.01, retry_count=1)])
    executor = make_executor(provider)
    make_run(executor, "run_cancel_retry", workflow)

    await executor.start("run_cancel_retry", workflow, {})

    assert provider.calls == 2
    assert provider.cleanup_done.is_set()
    assert not provider.retry_started_before_cleanup


@pytest.mark.asyncio
async def test_timeout_cancels_provider_before_step_finishes():
    provider = CancellableProvider()
    workflow = WorkflowDefinition(id="cancel", name="cancel", steps=[StepDefinition("a", agent_id="requirement_agent", task_template="hi", timeout_seconds=0.01, retry_count=0)])
    executor = make_executor(provider)
    make_run(executor, "run_cancel", workflow)

    await executor.start("run_cancel", workflow, {})

    assert provider.cancelled.is_set()


@pytest.mark.asyncio
async def test_timeout_and_missing_agent_fail_the_run():
    timeout_workflow = WorkflowDefinition(id="timeout", name="timeout", steps=[StepDefinition("a", agent_id="requirement_agent", task_template="hi", timeout_seconds=0.01, retry_count=0)])
    executor = make_executor(SlowProvider())
    make_run(executor, "run_timeout", timeout_workflow)
    await executor.start("run_timeout", timeout_workflow, {})
    timeout_run = executor.repository.get_run("run_timeout")
    assert timeout_run["status"] == RunStatus.FAILED.value
    assert timeout_run["steps"][0]["error_message"] == "Agent timed out after 0.01s"

    missing_workflow = WorkflowDefinition(id="missing", name="missing", steps=[StepDefinition("a", agent_id="does_not_exist", task_template="hi")])
    make_run(executor, "run_missing", missing_workflow)
    await executor.start("run_missing", missing_workflow, {})
    assert executor.repository.get_run("run_missing")["steps"][0]["status"] == "FAILED"


@pytest.mark.asyncio
async def test_downstream_step_is_skipped_after_dependency_failure():
    workflow = WorkflowDefinition(id="skip", name="skip", steps=[
        StepDefinition("bad", agent_id="does_not_exist", task_template="hi"),
        StepDefinition("downstream", agent_id="requirement_agent", depends_on=["bad"], task_template="hi"),
    ])
    executor = make_executor(RetryOnceProvider())
    make_run(executor, "run_skip", workflow)
    await executor.start("run_skip", workflow, {})
    statuses = {step["step_id"]: step["status"] for step in executor.repository.get_run("run_skip")["steps"]}
    assert statuses == {"bad": "FAILED", "downstream": "SKIPPED"}


@pytest.mark.asyncio
async def test_sse_history_contains_run_lifecycle_events():
    workflow = WorkflowDefinition(id="events", name="events", steps=[StepDefinition("a", agent_id="requirement_agent", task_template="hi")])
    executor = make_executor(RetryOnceProvider())
    make_run(executor, "run_events", workflow)
    await executor.start("run_events", workflow, {})
    event_types = [event["type"] async for event in executor.event_bus.stream("run_events")]
    assert event_types[0] == "workflow.started"
    assert "step.started" in event_types
    assert event_types[-1] == "workflow.completed"
