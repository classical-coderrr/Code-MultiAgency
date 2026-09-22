"""YAML DSL parser."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .models import StepDefinition, StepType, WorkflowDefinition


class WorkflowParseError(ValueError):
    pass


def parse_workflow_yaml(content: str, workflow_id: str | None = None) -> WorkflowDefinition:
    try:
        raw = yaml.safe_load(content)
    except yaml.YAMLError as exc:
        raise WorkflowParseError(f"Invalid workflow YAML: {exc}") from exc

    if not isinstance(raw, dict):
        raise WorkflowParseError("Workflow YAML must be an object")

    resolved_id = workflow_id or str(raw.get("id") or raw.get("name") or "workflow")
    raw_steps = raw.get("steps")
    if raw_steps is None:
        raw_steps = []
    if not isinstance(raw_steps, list):
        raise WorkflowParseError("Workflow steps must be a list")

    steps: list[StepDefinition] = []
    for index, item in enumerate(raw_steps):
        if not isinstance(item, dict):
            raise WorkflowParseError(f"Step at index {index} must be an object")
        step_id = item.get("id")
        if not isinstance(step_id, str) or not step_id.strip():
            raise WorkflowParseError(f"Step at index {index} requires a non-empty id")

        step_type = item.get("type", "agent")
        try:
            parsed_type = StepType(step_type)
        except ValueError as exc:
            raise WorkflowParseError(f"Unknown step type: {step_type}") from exc

        depends_on = item.get("depends_on", [])
        if isinstance(depends_on, str):
            depends_on = [depends_on]
        if not isinstance(depends_on, list) or not all(isinstance(dep, str) for dep in depends_on):
            raise WorkflowParseError(f"Step {step_id} depends_on must be a list of strings")

        retry_count, timeout_seconds = _parse_runtime_options(item)
        max_tokens = _parse_max_tokens(item.get("max_tokens"))
        thinking = _parse_thinking(item.get("thinking", "auto"))
        output_format = _parse_output_format(item.get("output_format", "text"))
        failure_policy = _parse_failure_policy(item.get("failure_policy", "fail"))
        runtime_plan = _parse_boolean(item.get("runtime_plan", False), f"Step {step_id} runtime_plan")
        generation_mode = _parse_generation_mode(item.get("generation_mode", "single"))
        skills = _parse_skills(item.get("skills", item.get("skill")))
        skill_mode = _parse_skill_mode(item.get("skill_mode", "auto"))
        enforce_artifact_contract = _parse_boolean(
            item.get("enforce_artifact_contract", False),
            f"Step {step_id} enforce_artifact_contract",
        )
        validation = _parse_validation(item.get("validation", {}), f"Step {step_id} validation")
        steps.append(
            StepDefinition(
                id=step_id,
                type=parsed_type,
                agent_id=item.get("agent") if isinstance(item.get("agent"), str) else None,
                task_template=str(item.get("task", "")),
                output=item.get("output") if isinstance(item.get("output"), str) else None,
                depends_on=depends_on,
                timeout_seconds=timeout_seconds,
                retry_count=retry_count,
                max_tokens=max_tokens,
                thinking=thinking,
                output_format=output_format,
                failure_policy=failure_policy,
                runtime_plan=runtime_plan,
                generation_mode=generation_mode,
                enforce_artifact_contract=enforce_artifact_contract,
                skills=skills,
                skill_mode=skill_mode,
                validation=validation,
            )
        )

    concurrency = raw.get("concurrency", 3)
    if not isinstance(concurrency, int):
        raise WorkflowParseError("Workflow concurrency must be an integer")

    return WorkflowDefinition(
        id=resolved_id,
        name=str(raw.get("name") or resolved_id),
        concurrency=concurrency,
        inputs=raw.get("inputs") if isinstance(raw.get("inputs"), dict) else {},
        steps=steps,
        meta=raw.get("meta") if isinstance(raw.get("meta"), dict) else {},
        runtime_defaults=_parse_runtime_defaults(raw.get("defaults", raw.get("runtime", {}))),
    )


def parse_workflow_file(path: str | Path, workflow_id: str | None = None) -> WorkflowDefinition:
    file_path = Path(path)
    return parse_workflow_yaml(file_path.read_text(encoding="utf-8"), workflow_id or file_path.stem)


def _parse_runtime_options(item: dict[str, Any]) -> tuple[int, float]:
    retry = item.get("retry", 2)
    if isinstance(retry, dict):
        retry = retry.get("max_attempts", retry.get("count", 2))
    try:
        retry_count = max(0, int(retry))
    except (TypeError, ValueError) as exc:
        raise WorkflowParseError(f"Step {item.get('id')} retry must be an integer") from exc

    timeout = item.get("timeout", 120)
    if isinstance(timeout, str) and timeout.endswith("s"):
        timeout = timeout[:-1]
    try:
        timeout_seconds = max(0.1, float(timeout))
    except (TypeError, ValueError) as exc:
        raise WorkflowParseError(f"Step {item.get('id')} timeout must be a number") from exc
    return retry_count, timeout_seconds


def _parse_max_tokens(value: Any) -> int | None:
    if value is None:
        return None
    try:
        max_tokens = int(value)
    except (TypeError, ValueError) as exc:
        raise WorkflowParseError("max_tokens must be an integer") from exc
    if not 1 <= max_tokens <= 128000:
        raise WorkflowParseError("max_tokens must be between 1 and 128000")
    return max_tokens


def _parse_thinking(value: Any) -> str:
    thinking = str(value or "auto").strip().lower()
    if thinking not in {"auto", "off", "low", "high", "max"}:
        raise WorkflowParseError("thinking must be one of auto, off, low, high, max")
    return thinking


def _parse_output_format(value: Any) -> str:
    output_format = str(value or "text").strip().lower()
    if output_format not in {"text", "markdown", "json", "html", "code"}:
        raise WorkflowParseError("output_format must be one of text, markdown, json, html, code")
    return output_format


def _parse_failure_policy(value: Any) -> str:
    policy = str(value or "fail").strip().lower()
    if policy not in {"fail", "continue", "fallback"}:
        raise WorkflowParseError("failure_policy must be one of fail, continue, fallback")
    return policy


def _parse_generation_mode(value: Any) -> str:
    mode = str(value or "single").strip().lower()
    if mode not in {"single", "artifacts", "auto"}:
        raise WorkflowParseError("generation_mode must be one of single, artifacts, auto")
    return mode


def _parse_skills(value: Any) -> list[str]:
    if value is None:
        return []
    raw_values = [value] if isinstance(value, str) else value
    if not isinstance(raw_values, list) or not all(isinstance(item, str) for item in raw_values):
        raise WorkflowParseError("skills must be a string or a list of strings")
    normalized: list[str] = []
    for raw in raw_values:
        skill_id = raw.strip()
        if not skill_id or not skill_id.replace("_", "").replace("-", "").isalnum():
            raise WorkflowParseError("Skill ids may contain only letters, numbers, hyphens and underscores")
        if skill_id not in normalized:
            normalized.append(skill_id)
    return normalized


def _parse_skill_mode(value: Any) -> str:
    # PyYAML follows YAML 1.1 and interprets unquoted ``on`` / ``off`` as
    # booleans.  Supporting that form keeps the declarative DSL natural.
    if isinstance(value, bool):
        return "on" if value else "off"
    mode = str(value or "auto").strip().lower()
    if mode not in {"auto", "on", "off"}:
        raise WorkflowParseError("skill_mode must be one of auto, on, off")
    return mode


def _parse_boolean(value: Any, field_name: str) -> bool:
    if isinstance(value, bool):
        return value
    raise WorkflowParseError(f"{field_name} must be a boolean")


def _parse_runtime_defaults(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise WorkflowParseError("Workflow defaults must be an object")
    defaults: dict[str, Any] = {}
    if "max_tokens" in value:
        defaults["max_tokens"] = _parse_max_tokens(value["max_tokens"])
    if "retry" in value:
        try:
            retry = int(value["retry"])
        except (TypeError, ValueError) as exc:
            raise WorkflowParseError("Workflow default retry must be an integer") from exc
        if not 0 <= retry <= 10:
            raise WorkflowParseError("Workflow default retry must be between 0 and 10")
        defaults["retry"] = retry
    if "thinking" in value:
        defaults["thinking"] = _parse_thinking(value["thinking"])
    if "skill_mode" in value:
        defaults["skill_mode"] = _parse_skill_mode(value["skill_mode"])
    return defaults


def _parse_validation(value: Any, field_name: str) -> dict[str, Any]:
    """Parse an extensible validation profile without accepting code strings."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise WorkflowParseError(f"{field_name} must be an object")
    profile = dict(value)
    if "enabled" in profile and not isinstance(profile["enabled"], bool):
        raise WorkflowParseError(f"{field_name}.enabled must be a boolean")
    if "targets" in profile:
        targets = profile["targets"]
        if isinstance(targets, str):
            targets = [targets]
        if not isinstance(targets, list) or not all(isinstance(item, str) for item in targets):
            raise WorkflowParseError(f"{field_name}.targets must be a string or a list of strings")
        profile["targets"] = [str(item).strip().lower() for item in targets if str(item).strip()]
    for key in ("timeout_seconds", "startup_timeout_seconds"):
        if key in profile:
            try:
                profile[key] = max(1.0, float(profile[key]))
            except (TypeError, ValueError) as exc:
                raise WorkflowParseError(f"{field_name}.{key} must be a number") from exc
    if "repair_attempts" in profile:
        try:
            profile["repair_attempts"] = max(0, min(4, int(profile["repair_attempts"])))
        except (TypeError, ValueError) as exc:
            raise WorkflowParseError(f"{field_name}.repair_attempts must be an integer") from exc
    for key in ("build", "startup", "install_dependencies"):
        if key in profile and not isinstance(profile[key], bool):
            raise WorkflowParseError(f"{field_name}.{key} must be a boolean")
    return profile
