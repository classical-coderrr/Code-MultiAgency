from __future__ import annotations

import hashlib
import json
import time
from functools import wraps
from pathlib import Path
from typing import Any

from ..llm.base import LLMResponse
from .models import StepDefinition, StepStatus, StepType, utc_now
from .run_state import RunState
from .runtime_policy import RuntimePolicyResolver
from .workspace_artifacts import WorkspaceArtifactCollector


def provider_run_scope(function):
    @wraps(function)
    async def wrapped(self, state, *args, **kwargs):
        with self.model_invocation.run_scope(state.run_id):
            return await function(self, state, *args, **kwargs)
    return wrapped


class ExecutorSupportMixin:
    """Small policy and bookkeeping helpers shared by the workflow executor."""

    @staticmethod
    def _artifact_progress(state: RunState, step_id: str) -> tuple[str, list[dict[str, str]]]:
        snapshot = state.context.snapshot()
        fingerprint = hashlib.sha256(json.dumps({
            "contract": snapshot.get("compiled_contract"),
            "requirement": state.requirement_spec.model_dump(mode="json") if state.requirement_spec else None,
            "step": step_id,
        }, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest()
        progress = snapshot.get("__artifact_progress__")
        entry = progress.get(step_id) if isinstance(progress, dict) else None
        files = list(entry.get("files") or []) if isinstance(entry, dict) and entry.get("fingerprint") == fingerprint else []
        return fingerprint, files

    async def _checkpoint_artifact_file(self, state: RunState, step_id: str, fingerprint: str, file_record: dict[str, str]) -> int:
        async with state.artifact_progress_lock:
            return self._checkpoint_artifact_file_locked(state, step_id, fingerprint, file_record)

    def _checkpoint_artifact_file_locked(self, state: RunState, step_id: str, fingerprint: str, file_record: dict[str, str]) -> int:
        current = state.context.snapshot().get("__artifact_progress__")
        updated = dict(current) if isinstance(current, dict) else {}
        entry = updated.get(step_id)
        preserved = list(entry.get("files") or []) if isinstance(entry, dict) and entry.get("fingerprint") == fingerprint else []
        name = str(file_record.get("name") or "").casefold()
        preserved = [row for row in preserved if isinstance(row, dict) and str(row.get("name") or "").casefold() != name]
        preserved.append(dict(file_record))
        updated[step_id] = {"fingerprint": fingerprint, "files": preserved}
        state.context.set("__artifact_progress__", updated)
        self._persist_state(state)
        return len(preserved)

    @staticmethod
    def _clear_artifact_progress(state: RunState, step_id: str) -> None:
        saved = state.context.snapshot().get("__artifact_progress__")
        if isinstance(saved, dict) and step_id in saved:
            updated = dict(saved)
            updated.pop(step_id, None)
            state.context.set("__artifact_progress__", updated)

    @classmethod
    def _safe_snapshot_value(cls, value: Any, key: str = "") -> Any:
        """Keep recovery snapshots useful without persisting credential fields."""
        sensitive = {"api_key", "apikey", "authorization", "cookie", "password", "secret", "token"}
        if key.lower() in sensitive:
            return "[REDACTED]"
        if isinstance(value, dict):
            return {str(item_key): cls._safe_snapshot_value(item_value, str(item_key)) for item_key, item_value in value.items()}
        if isinstance(value, list):
            return [cls._safe_snapshot_value(item) for item in value]
        if isinstance(value, (str, int, float, bool)) or value is None:
            return value
        return str(value)

    def _persist_state(self, state: RunState) -> None:
        state.snapshot_revision += 1
        snapshot = {
            "version": 2,
            "snapshot_revision": state.snapshot_revision,
            "run_id": state.run_id,
            "context": self._safe_snapshot_value(state.context.snapshot()),
            "results": {key: value.value for key, value in state.results.items()},
            "requirement_spec": state.requirement_spec.model_dump(mode="json") if state.requirement_spec else None,
            "runtime": self._safe_snapshot_value(state.runtime),
            "current_level": state.current_level,
            "waiting_step_id": state.waiting_step_id,
            "adaptive_policy": self._safe_snapshot_value(state.adaptive_policy),
            "runtime_budget_plan": self._safe_snapshot_value(state.runtime_budget_plan),
            "policy_skipped_steps": sorted(state.policy_skipped_steps),
            "checkpoint_thread_id": state.checkpoint_thread_id or state.run_id,
            "recovery_count": state.recovery_count,
            "clarification_request": self._safe_snapshot_value(state.clarification_request),
            "blueprint": self._safe_snapshot_value(state.blueprint),
            "workspace": self._safe_snapshot_value(state.workspace),
            "execution_status": state.execution_status,
            "delivery_status": state.delivery_status,
        }
        if not self.repository.save_run_snapshot(state.run_id, snapshot, heartbeat_at=utc_now()):
            raise RuntimeError(f"Rejected stale Run snapshot revision {state.snapshot_revision}")

    @staticmethod
    def _begin_approval_wait(state: RunState) -> None:
        if state.approval_started_perf is None:
            state.approval_started_perf = time.perf_counter()

    @staticmethod
    def _end_approval_wait(state: RunState) -> None:
        if state.approval_started_perf is None:
            return
        state.excluded_approval_ms += max(
            0,
            int((time.perf_counter() - state.approval_started_perf) * 1000),
        )
        state.approval_started_perf = None

    @staticmethod
    def _approval_duration_ms(state: RunState) -> int:
        current_wait = (
            int((time.perf_counter() - state.approval_started_perf) * 1000)
            if state.approval_started_perf is not None
            else 0
        )
        return max(0, state.excluded_approval_ms + current_wait)

    @classmethod
    def _active_duration_ms(cls, state: RunState) -> int:
        wall_duration = int((time.perf_counter() - state.started_perf) * 1000)
        return max(0, wall_duration - cls._approval_duration_ms(state))

    @staticmethod
    def _delivery_proof(state: RunState) -> dict[str, Any]:
        proof = dict(state.context.snapshot().get("artifact_validation") or {})
        checks = [item for item in proof.get("checks", []) if isinstance(item, dict)]
        approval_steps = [step for step in state.workflow.steps if step.type == StepType.APPROVAL]
        if approval_steps and not any(item.get("id") == "capability-human_approval" for item in checks):
            checks.append(
                {
                    "id": "capability-human_approval",
                    "status": "passed"
                    if all(state.results.get(step.id) == StepStatus.SUCCESS for step in approval_steps)
                    else "blocked",
                    "message": "人工审批结果",
                }
            )
        proof["checks"] = checks
        return proof

    def _runtime_for_step(self, state: RunState, step: StepDefinition) -> dict[str, Any]:
        """Compatibility shim for extensions that called the former helper."""
        return self.runtime_policy.resolve(state, step)

    @staticmethod
    def _runtime_plan_target_step_ids(state: RunState, planner_step_id: str) -> set[str]:
        return RuntimePolicyResolver.runtime_plan_target_step_ids(state, planner_step_id)

    @staticmethod
    def _confirmed_requirement_for_budget(state: RunState) -> str:
        return RuntimePolicyResolver.confirmed_requirement_for_budget(state)

    @staticmethod
    def _generation_mode(state: RunState, step: StepDefinition) -> str:
        return RuntimePolicyResolver.generation_mode(state, step)

    @staticmethod
    def _coding_loop_budget(
        file_plan: list[Any],
        owner: str,
        configured_tokens: int,
        provider_tokens: int,
        budget_mode: str,
    ) -> int:
        """Return a bounded Agent-wide budget derived from its frozen files in Auto mode."""
        configured = max(1, int(configured_tokens))
        if str(budget_mode).strip().lower() != "auto":
            return configured
        per_file_ceiling = max(1, min(configured, int(provider_tokens)))
        estimates = {
            ".java": 2600, ".vue": 3200, ".tsx": 2600, ".ts": 2200,
            ".html": 2000, ".jsx": 2400, ".js": 1800, ".css": 1800,
            ".scss": 1800, ".sql": 1900, ".xml": 1500, ".json": 1200,
            ".yml": 1200, ".yaml": 1200, ".properties": 900,
            ".md": 900, ".txt": 700,
        }
        planned = [
            row for row in file_plan
            if isinstance(row, dict)
            and str(row.get("owner") or "").strip().casefold() == owner.casefold()
            and str(row.get("path") or "").strip()
        ]
        if not planned:
            return configured
        per_file = 0
        for row in planned:
            suffix = Path(str(row.get("path") or "")).suffix.lower()
            per_file += min(per_file_ceiling, estimates.get(suffix, 1400))
        protocol_reserve = min(4000, 400 + 160 * len(planned))
        return min(128000, max(configured, per_file + protocol_reserve))

    @staticmethod
    def _normalize_artifact_manifest(
        files: list[Any], file_plan: list[Any],
    ) -> list[dict[str, Any]]:
        """Deduplicate recoverable legacy rows while enforcing frozen ownership."""
        rows = [item for item in files if isinstance(item, dict)]
        owners = {
            str(item.get("owner") or "").strip().casefold()
            for item in file_plan if isinstance(item, dict) and str(item.get("owner") or "").strip()
        }
        owners.update(
            str(item.get("step_id") or item.get("owner_step") or "").strip().casefold()
            for item in rows
            if str(item.get("step_id") or item.get("owner_step") or "").strip()
        )
        normalized: list[dict[str, Any]] = []
        for owner in sorted(owners):
            owned = [
                item for item in rows
                if str(item.get("step_id") or item.get("owner_step") or "").strip().casefold() == owner
            ]
            normalized = WorkspaceArtifactCollector.merge_owner_artifacts(
                normalized,
                owned,
                owner,
                file_plan=file_plan,
            )
        return normalized

    async def _skip_by_policy(self, state: RunState, step: StepDefinition) -> None:
        skip_reason = str(
            state.adaptive_policy.get("skip_reasons", {}).get(step.id)
            or "Architecture 与需求能力路由确认该步骤不是必需能力。"
        )
        state.results[step.id] = StepStatus.SKIPPED
        if step.output:
            state.context.set(step.output, f"Not required: {skip_reason}")
            state.context.set(f"{step.output}_files", "[]")
        self.repository.upsert_step(
            state.run_id,
            step.id,
            agent_id=step.agent_id,
            status=StepStatus.SKIPPED.value,
            finished_at=utc_now(),
            error_message=f"Skipped by adaptive capability policy: {skip_reason}",
        )
        await self.event_bus.emit(
            "step.skipped",
            state.run_id,
            {
                "stepId": step.id,
                "status": StepStatus.SKIPPED.value,
                "reason": skip_reason,
            },
        )

    @staticmethod
    def _dependency_satisfied(state: RunState, dependency: str, steps: dict[str, StepDefinition]) -> bool:
        status = state.results.get(dependency)
        return status == StepStatus.SUCCESS or (status == StepStatus.SKIPPED and dependency in state.policy_skipped_steps) or (status == StepStatus.FAILED and steps[dependency].failure_policy == "continue")


def _thinking_for_attempt(initial: str, retry_count: int) -> str:
    """Return a strictly non-increasing thinking level for each attempt."""
    level = str(initial).strip().lower()
    if retry_count <= 0:
        return level
    if retry_count == 1 and level in {"high", "max"}:
        return "low"
    return "off"


def _reasoning_budget_exhausted(response: LLMResponse, max_tokens: int) -> bool:
    """Detect a response that spent essentially all output budget on reasoning."""
    if response.text.strip():
        return False
    usage = response.usage if isinstance(response.usage, dict) else {}
    completion_tokens = int(usage.get("completion_tokens", response.output_tokens or 0) or 0)
    details = usage.get("completion_tokens_details")
    reasoning_tokens = int(details.get("reasoning_tokens", 0) or 0) if isinstance(details, dict) else 0
    reached_limit = response.finish_reason == "length" or completion_tokens >= max(1, int(max_tokens * 0.95))
    reasoning_dominated = reasoning_tokens >= max(1, int(completion_tokens * 0.9)) if completion_tokens else False
    return reached_limit and (reasoning_dominated or bool((response.reasoning_content or "").strip()))


def _can_use_structured_fallback(response: LLMResponse | None, error_message: str) -> bool:
    """Allow local recovery only for response-format failures, not transport errors."""
    if response is None:
        return False
    if str(response.finish_reason or "").strip().lower() == "length":
        return True
    if response.reasoning_content and not response.text.strip():
        return True
    normalized = error_message.lower()
    return any(
        marker in normalized
        for marker in (
            "output validation failed",
            "invalid_json",
            "empty output",
            "truncated",
            "query_parameters",
            "attributeerror",
            "object has no attribute",
            "expected a valid json object",
            "实体合同 fields",
            "字段名到类型的映射",
            "实体字段",
            "类型未明确",
        )
    )


def _continuation_prompt(original_prompt: str, partial_output: str) -> str:
    return (
        f"{original_prompt}\n\n"
        "上一次输出已达到 Provider 的长度上限，以下是已保存的部分输出。"
        "请只从末尾继续生成缺失内容，不要重复已有内容，不要重新开始：\n"
        "<partial_output>\n"
        f"{partial_output}\n"
        "</partial_output>\n"
        "请只输出新增内容。"
    )


def _merge_continuation(previous: str, current: str, output_format: str) -> str:
    if not previous:
        return current
    if not current:
        return previous
    max_overlap = min(len(previous), len(current), 2000)
    for size in range(max_overlap, 0, -1):
        if previous[-size:] == current[:size]:
            return previous + current[size:]
    separator = "\n" if output_format in {"text", "markdown"} else ""
    return previous + separator + current


def _merge_usage(previous: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    merged = dict(previous)
    for key, value in current.items():
        if isinstance(value, (int, float)) and isinstance(merged.get(key), (int, float)):
            merged[key] = merged[key] + value
        elif key not in merged:
            merged[key] = value
    return merged
