import asyncio

from app.agents.registry import AgentRegistry
from app.llm.base import LLMProvider, LLMResponse
from app.repositories.sqlite import SQLiteRepository
from app.skills.registry import SkillDefinition, SkillRegistry
from app.skills.resolver import SkillResolver
from app.workflow.events import WorkflowEventBus
from app.workflow.executor import WorkflowExecutor
from app.workflow.models import RunStatus, StepDefinition, WorkflowDefinition
from app.workflow.parser import WorkflowParseError, parse_workflow_yaml
from app.workflow.requirements import RequirementSpec


def test_skill_registry_reads_front_matter_and_ignores_invalid_optional_skill(tmp_path):
    root = tmp_path / "skills"
    valid = root / "api-contract"
    valid.mkdir(parents=True)
    (valid / "SKILL.md").write_text(
        "---\nid: api-contract\nversion: '2.0'\nmin_complexity: low\n---\nUse a stable API contract.",
        encoding="utf-8",
    )
    invalid = root / "bad"
    invalid.mkdir()
    (invalid / "SKILL.md").write_text("---\nid: not allowed!\n---\nBroken", encoding="utf-8")

    registry = SkillRegistry.from_directories([root])

    assert registry.get("api-contract") is not None
    assert registry.get("api-contract").version == "2.0"
    assert registry.get("not allowed!") is None


def test_skill_resolver_auto_uses_complexity_and_manual_mode_overrides_it():
    registry = SkillRegistry([
        SkillDefinition(
            id="api-contract",
            applies_to=("backend",),
            capabilities=("backend",),
            min_complexity="medium",
            body="Keep API contracts explicit.",
        )
    ])
    resolver = SkillResolver(registry)
    step = StepDefinition("backend", agent_id="backend_agent", skills=["api-contract"])
    low = RequirementSpec(raw_requirement="做一个简单静态页面", required_capabilities=["frontend"])
    high = RequirementSpec(raw_requirement="开发一个多模块学生管理系统，需要前后端、数据库、CRUD 和权限", required_capabilities=["backend", "frontend", "persistence"])

    low_result = resolver.resolve(step, low, {"skill_mode": "auto"}, "BASE")
    high_result = resolver.resolve(step, high, {"skill_mode": "auto"}, "BASE")
    forced_result = resolver.resolve(step, low, {"skill_mode": "on"}, "BASE")

    assert not low_result.skills
    assert low_result.skipped[0]["reason"] in {"capability_not_required", "task_below_skill_complexity"}
    assert [skill.id for skill in high_result.skills] == ["api-contract"]
    assert "Dynamic Skill: api-contract" in high_result.system_prompt
    assert [skill.id for skill in forced_result.skills] == ["api-contract"]


def test_workflow_parser_accepts_skill_alias_and_validates_skill_mode():
    workflow = parse_workflow_yaml(
        """
name: example
defaults:
  skill_mode: off
steps:
  - id: implementation
    agent: backend_agent
    skill: api-contract
    skill_mode: on
"""
    )

    assert workflow.runtime_defaults["skill_mode"] == "off"
    assert workflow.steps[0].skills == ["api-contract"]
    assert workflow.steps[0].skill_mode == "on"

    try:
        parse_workflow_yaml("name: example\nsteps:\n  - id: a\n    agent: backend_agent\n    skill_mode: maybe")
    except WorkflowParseError as exc:
        assert "skill_mode" in str(exc)
    else:
        raise AssertionError("Invalid Skill mode must not parse")


class _PromptCaptureProvider(LLMProvider):
    def __init__(self) -> None:
        self.system_prompts: list[str] = []

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
        self.system_prompts.append(system_prompt)
        return LLMResponse("complete", finish_reason="stop", message_content="complete")


def test_executor_injects_resolved_skill_and_persists_audit():
    provider = _PromptCaptureProvider()
    repository = SQLiteRepository(":memory:")
    repository.create_run("run_skill", "skill-workflow", {}, RunStatus.PENDING.value, "now")
    workflow = WorkflowDefinition(
        id="skill-workflow",
        name="skill workflow",
        steps=[
            StepDefinition(
                "requirement",
                agent_id="requirement_agent",
                task_template="{{requirement}}",
                output="result",
                skills=["scope"],
            )
        ],
    )
    registry = SkillRegistry([
        SkillDefinition(id="scope", applies_to=("requirement",), min_complexity="low", body="Keep scope testable."),
    ])
    executor = WorkflowExecutor(
        AgentRegistry.from_directory("agents"),
        provider,
        WorkflowEventBus(),
        repository,
        skill_registry=registry,
    )

    asyncio.run(executor.start("run_skill", workflow, {"requirement": "开发一个简单页面"}, {"defaults": {"skill_mode": "on"}}))

    run = repository.get_run("run_skill")
    assert run["status"] == RunStatus.SUCCESS.value
    assert "Dynamic Skill: scope" in provider.system_prompts[0]
    assert run["state"]["context"]["__skill_resolutions__"]["requirement"]["applied"][0]["id"] == "scope"
    assert any(event["type"] == "step.skills_resolved" for event in executor.event_bus._history["run_skill"])
