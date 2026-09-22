"""Resolve per-step runtime, budget, and artifact-generation policies."""

from __future__ import annotations

import json
from typing import Any

from .adaptive import BOOTSTRAP_BUDGETS, bootstrap_budget_for_step
from .models import StepDefinition, StepStatus, StepType
from .run_state import RunState


class RuntimePolicyResolver:
    """Pure policy component; it performs no provider or repository I/O."""

    def resolve(self, state: RunState, step: StepDefinition) -> dict[str, Any]:
        defaults = {**state.workflow.runtime_defaults}
        request_defaults = (
            state.runtime.get("defaults", {}) if isinstance(state.runtime, dict) else {}
        )
        if isinstance(request_defaults, dict):
            defaults.update(request_defaults)
        resolved: dict[str, Any] = {
            "max_tokens": step.max_tokens or defaults.get("max_tokens") or 6000,
            "retry": step.retry_count,
            "thinking": (
                defaults.get("thinking", "auto")
                if step.thinking == "auto"
                else step.thinking
            ),
            "skill_mode": (
                defaults.get("skill_mode", "auto")
                if step.skill_mode == "auto"
                else step.skill_mode
            ),
        }
        if "retry" in defaults and step.retry_count == 2:
            resolved["retry"] = defaults["retry"]
        if "max_tokens" in defaults and step.max_tokens is None:
            resolved["max_tokens"] = defaults["max_tokens"]
        step_overrides = (
            state.runtime.get("steps", {}).get(step.id, {})
            if isinstance(state.runtime, dict)
            else {}
        )
        if isinstance(step_overrides, dict):
            resolved.update(
                {key: value for key, value in step_overrides.items() if value is not None}
            )
        try:
            resolved["max_tokens"] = min(
                128000, max(1, int(resolved["max_tokens"]))
            )
            resolved["retry"] = min(10, max(0, int(resolved["retry"])))
        except (TypeError, ValueError):
            resolved["max_tokens"] = 6000
            resolved["retry"] = step.retry_count
        resolved["thinking"] = str(resolved.get("thinking", "auto")).strip().lower()
        if resolved["thinking"] not in {"auto", "off", "low", "high", "max"}:
            resolved["thinking"] = "auto"
        resolved["skill_mode"] = str(
            resolved.get("skill_mode", "auto")
        ).strip().lower()
        if resolved["skill_mode"] not in {"auto", "on", "off"}:
            resolved["skill_mode"] = "auto"

        if step.runtime_plan:
            # The planner is a control step; UI overrides must not alter its
            # bounded budget, retry count, or thinking policy.
            if step.max_tokens is not None:
                resolved["max_tokens"] = int(step.max_tokens)
            resolved["retry"] = int(step.retry_count)
            resolved["thinking"] = "off" if step.thinking == "auto" else step.thinking

        configured_max_tokens = int(resolved["max_tokens"])
        budget_mode = (
            str(state.runtime.get("budget_mode", "auto")).strip().lower()
            if isinstance(state.runtime, dict)
            else "auto"
        )
        if budget_mode not in {"auto", "manual"}:
            budget_mode = "auto"
        resolved["budget_mode"] = budget_mode
        if budget_mode == "auto":
            adaptive_budget = None
            budget_source = None
            difficulty = None
            difficulty_score = None
            difficulty_signals: list[str] = []
            planner_budget = (
                state.runtime_budget_plan.get("agent_budgets", {}).get(step.id)
                if state.runtime_budget_plan
                else None
            )
            if planner_budget is not None:
                adaptive_budget = int(planner_budget)
                budget_source = "runtime_plan_agent"
                difficulty = str(state.runtime_budget_plan.get("difficulty", "medium"))
                rationale = state.runtime_budget_plan.get("rationale", [])
                if isinstance(rationale, list):
                    difficulty_signals = [str(item) for item in rationale[:5]]
            if state.adaptive_policy:
                architecture_budget = state.adaptive_policy.get("budgets", {}).get(step.id)
                if architecture_budget is not None:
                    adaptive_budget = max(
                        int(adaptive_budget or 0), int(architecture_budget)
                    )
                    budget_source = (
                        "runtime_plan_agent+architecture_policy"
                        if planner_budget is not None
                        else "architecture_policy"
                    )
                    architecture_difficulty = str(
                        state.adaptive_policy.get("complexity", "medium")
                    )
                    levels = {"low": 0, "medium": 1, "high": 2}
                    if levels.get(architecture_difficulty, 1) > levels.get(
                        str(difficulty), -1
                    ):
                        difficulty = architecture_difficulty
            if adaptive_budget is None:
                requirement = str(state.context.snapshot().get("requirement", ""))
                adaptive_budget, assessment = bootstrap_budget_for_step(
                    step.id, requirement
                )
                if adaptive_budget is not None:
                    budget_source = "input_difficulty"
                    difficulty = assessment["level"]
                    difficulty_score = assessment["score"]
                    difficulty_signals = list(assessment.get("signals", []))
            if adaptive_budget is None:
                adaptive_budget = BOOTSTRAP_BUDGETS.get(step.id)
                if adaptive_budget is not None:
                    budget_source = "workflow_fallback"
            if adaptive_budget is not None:
                resolved["max_tokens"] = min(
                    resolved["max_tokens"], int(adaptive_budget)
                )
                resolved["_budget_plan"] = {
                    "source": budget_source or "adaptive",
                    "difficulty": difficulty,
                    "difficulty_score": difficulty_score,
                    "difficulty_signals": difficulty_signals,
                    "configured_max_tokens": configured_max_tokens,
                    "recommended_max_tokens": int(adaptive_budget),
                    "planned_max_tokens": int(resolved["max_tokens"]),
                }
            if resolved["thinking"] == "auto":
                planner_thinking = (
                    state.runtime_budget_plan.get("thinking", {}).get(step.id)
                    if state.runtime_budget_plan
                    else None
                )
                if planner_thinking in {"off", "low", "high", "max"}:
                    resolved["thinking"] = str(planner_thinking)
                elif state.adaptive_policy:
                    resolved["thinking"] = str(
                        state.adaptive_policy.get("thinking", {}).get(step.id, "low")
                    )
                elif step.id in {"requirement", "architecture"}:
                    resolved["thinking"] = "low"
        resolved["_configured_max_tokens"] = configured_max_tokens
        return resolved

    @staticmethod
    def runtime_plan_target_step_ids(
        state: RunState, planner_step_id: str
    ) -> set[str]:
        return {
            item.id
            for item in state.workflow.steps
            if item.type == StepType.AGENT
            and item.id != planner_step_id
            and state.results.get(item.id)
            not in {StepStatus.SUCCESS, StepStatus.SKIPPED}
        }

    @staticmethod
    def confirmed_requirement_for_budget(state: RunState) -> str:
        context = state.context.snapshot()
        return json.dumps(
            {
                "raw_requirement": context.get("requirement", ""),
                "requirement_spec": context.get("requirement_spec", {}),
                "requirement_summary": context.get("requirement_doc", ""),
                "clarification_answers": context.get("clarification_answers", {}),
            },
            ensure_ascii=False,
        )

    @staticmethod
    def generation_mode(state: RunState, step: StepDefinition) -> str:
        mode = str(step.generation_mode or "single").strip().lower()
        runtime = state.runtime if isinstance(state.runtime, dict) else {}
        if step.id in {"database", "backend", "frontend"} and (
            bool(runtime.get("coding_loop"))
            or str((state.workspace or {}).get("mode", "")) == "existing_repo"
        ):
            return "coding_loop"
        if step.enforce_artifact_contract and step.id in {
            "database",
            "backend",
            "frontend",
        }:
            return "artifacts"
        if mode in {"single", "artifacts"}:
            return mode
        if step.output_format in {"html", "code"}:
            return "artifacts"
        requirement = str(state.context.snapshot().get("requirement", "")).strip().lower()
        if not requirement:
            return "single"
        explicit_code_terms = (
            "源码", "代码", "写一个", "编写", "开发一个", "实现一个", "搭建",
            "生成文件", "网页", "网站", "应用", "html", "frontend", "backend",
            "implement", "build", "create", "code",
        )
        planning_terms = ("分析", "方案", "规划", "评估", "总结", "review", "plan")
        implementation_terms = (
            "写", "开发", "实现", "生成", "搭建", "代码", "源码", "build",
            "implement", "code",
        )
        if any(term in requirement for term in explicit_code_terms) and (
            not any(term in requirement for term in planning_terms)
            or any(term in requirement for term in implementation_terms)
        ):
            return "artifacts"
        return "single"

