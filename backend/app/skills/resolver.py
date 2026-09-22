"""Deterministically select prompt Skills without an additional LLM call."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..workflow.adaptive import estimate_requirement_difficulty
from ..workflow.models import StepDefinition
from ..workflow.requirements import RequirementSpec
from .registry import SkillDefinition, SkillRegistry


_LEVELS = {"low": 0, "medium": 1, "high": 2}
_MODES = {"auto", "on", "off"}


@dataclass(frozen=True, slots=True)
class SkillResolution:
    requested_mode: str
    resolved_mode: str
    skills: tuple[SkillDefinition, ...]
    missing: tuple[str, ...]
    skipped: tuple[dict[str, str], ...]
    system_prompt: str
    injection_chars: int
    difficulty: str

    def audit_payload(self) -> dict[str, Any]:
        return {
            "requestedMode": self.requested_mode,
            "resolvedMode": self.resolved_mode,
            "applied": [
                {"id": skill.id, "version": skill.version, "source": skill.source}
                for skill in self.skills
            ],
            "missing": list(self.missing),
            "skipped": list(self.skipped),
            "injectionChars": self.injection_chars,
            "difficulty": self.difficulty,
        }


class SkillResolver:
    """Resolve only declared candidate Skills, capped at two per Agent call."""

    def __init__(self, registry: SkillRegistry, max_skills_per_step: int = 2) -> None:
        self.registry = registry
        self.max_skills_per_step = max(1, max_skills_per_step)

    def resolve(
        self,
        step: StepDefinition,
        requirement_spec: RequirementSpec | None,
        runtime: dict[str, Any],
        system_prompt: str,
    ) -> SkillResolution:
        requested_mode = str(runtime.get("skill_mode", step.skill_mode or "auto")).strip().lower()
        if requested_mode not in _MODES:
            requested_mode = "auto"
        raw_requirement = requirement_spec.raw_requirement if requirement_spec else ""
        assessment = estimate_requirement_difficulty(raw_requirement)
        difficulty = str(assessment.get("level", "low"))
        candidates = list(dict.fromkeys(step.skills))
        if requested_mode == "off" or not candidates:
            return SkillResolution(requested_mode, "off", (), (), (), system_prompt, 0, difficulty)

        capabilities = set(requirement_spec.required_capabilities if requirement_spec else ())
        selected: list[SkillDefinition] = []
        missing: list[str] = []
        skipped: list[dict[str, str]] = []
        for skill_id in candidates:
            skill = self.registry.get(skill_id)
            if skill is None:
                missing.append(skill_id)
                continue
            if len(selected) >= self.max_skills_per_step:
                skipped.append({"id": skill.id, "reason": "step_skill_limit"})
                continue
            if requested_mode == "auto":
                applies = not skill.applies_to or step.id in skill.applies_to or (step.agent_id or "") in skill.applies_to
                capability_match = not skill.capabilities or bool(capabilities.intersection(skill.capabilities))
                complex_enough = _LEVELS.get(difficulty, 0) >= _LEVELS.get(skill.min_complexity, 1)
                if not applies:
                    skipped.append({"id": skill.id, "reason": "not_applicable_to_step"})
                    continue
                if not capability_match:
                    skipped.append({"id": skill.id, "reason": "capability_not_required"})
                    continue
                if not skill.always and not complex_enough:
                    skipped.append({"id": skill.id, "reason": "task_below_skill_complexity"})
                    continue
            selected.append(skill)

        if not selected:
            return SkillResolution(requested_mode, "off", (), tuple(missing), tuple(skipped), system_prompt, 0, difficulty)

        parts: list[str] = []
        for skill in selected:
            # Character budget is intentional: it is provider-independent and
            # avoids wasting a second tokenization/model call just for routing.
            body = skill.body[: skill.max_injection_tokens * 4].strip()
            if body:
                parts.append(f"## Dynamic Skill: {skill.id} (v{skill.version})\n{body}")
        appendix = "\n\n".join(parts)
        prompt = f"{system_prompt.rstrip()}\n\n---\n{appendix}" if appendix else system_prompt
        return SkillResolution(
            requested_mode,
            "on",
            tuple(selected),
            tuple(missing),
            tuple(skipped),
            prompt,
            len(appendix),
            difficulty,
        )
