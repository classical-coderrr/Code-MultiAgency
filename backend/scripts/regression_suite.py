"""Isolated code-company regression runner (no model calls without --live).

From the repository root::

    rtk proxy python backend/scripts/regression_suite.py
    rtk proxy python backend/scripts/regression_suite.py --live --auto-approve --repeat 2

MODEL_* configuration is read from the environment or backend/.env only in
live workers. --cases accepts a JSON list of {id, requirement, expected_stack}.
Each fresh Run retains its SQLite database, frozen YAML, evidence and workspace.
Synthetic cases may supply clarification_answers for explicitly chosen scope;
unanswered clarification ends that case as NEEDS_INPUT instead of timing out.
SUCCESS is the executor status; deliverable additionally requires deterministic
gate evidence and the requested stack. Unknown delivery fields fail closed.
first_pass means deliverable with no repairs, retries, continuations, fallback
or Run recovery across all attempts; null means the attempt evidence is missing.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib
import json
import math
import os
import re
import shutil
import signal
import statistics
import subprocess
import sys
import time
import uuid
from dataclasses import asdict, dataclass
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit, urlunsplit

BACKEND = Path(__file__).resolve().parents[1]
TERMINAL = {"SUCCESS", "FAILED", "STOPPED", "CANCELLED"}
STACKS = {"springboot_html", "springboot_vue", "static_html"}
_SECRET_VALUES: set[str] = set()


@dataclass(frozen=True)
class Case:
    id: str
    requirement: str
    expected_stack: str
    clarification_answers: dict[str, Any] | None = None
    difficulty: str = ""
    cohort: str = "known"


DEFAULT_CASES = (
    Case("springboot-html", "生成可运行的学生管理系统完整源码：Spring Boot + 原生 HTML/CSS/JavaScript，"
         "页面由 Spring Boot static 目录提供，REST API 实现学生增删改查，字段 id/name/email。"
         "使用 JPA，测试使用 H2 内存数据库；提供 Maven 测试及启动说明。不要登录或其他扩展功能。",
         "springboot_html"),
    Case("springboot-vue", "生成可运行的学生管理系统完整源码：Spring Boot REST API + Vue 3/Vite。"
         "实现学生增删改查，前后端共享 id/name/email 字段和 API 路径，配置前端代理。"
         "使用 JPA，测试使用 H2 内存数据库；提供 Maven 测试、npm 构建及启动说明。不要登录或扩展功能。",
         "springboot_vue"),
    Case("static-html", "生成完整的响应式待办事项静态 HTML 页面源码，纯前端、无需后端或数据库，"
         "使用 localStorage 保存待办，支持新增、完成、删除，提供可直接打开的 index.html。",
         "static_html"),
)


def load_cases(path: Path | None = None, *, cohort: str = "known") -> tuple[Case, ...]:
    if cohort not in {"known", "unseen"}:
        raise ValueError("cohort must be known or unseen")
    if path is None:
        return DEFAULT_CASES
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list) or not raw:
        raise ValueError("cases must be a nonempty JSON list")
    cases = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("each case must be an object")
        answers = item.get("clarification_answers")
        if answers is not None and (not isinstance(answers, dict) or any(not isinstance(key, str) for key in answers)):
            raise ValueError("clarification_answers must be an object with string keys")
        case = Case(
            item.get("id", ""), item.get("requirement", ""),
            item.get("expected_stack", ""), answers,
            str(item.get("difficulty", "")).strip().upper(), cohort,
        )
        if not isinstance(case.id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", case.id):
            raise ValueError("invalid case id")
        if not isinstance(case.requirement, str) or not case.requirement.strip():
            raise ValueError("case requirement must be nonempty")
        if not isinstance(case.expected_stack, str) or case.expected_stack not in STACKS:
            raise ValueError("unsupported expected_stack")
        if case.difficulty and not re.fullmatch(r"L[1-9][0-9]*", case.difficulty):
            raise ValueError("difficulty must use the form L1, L2, ...")
        cases.append(case)
    if len({case.id for case in cases}) != len(cases):
        raise ValueError("duplicate case id")
    return tuple(cases)


def _safe_base_url(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        parsed = urlsplit(raw)
        host = parsed.hostname or ""
        if parsed.port:
            host += f":{parsed.port}"
        return urlunsplit((parsed.scheme, host, parsed.path.rstrip("/"), "", ""))
    except ValueError:
        return "[invalid-url]"


def provider_snapshot(provider: Any, environment: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Return a stable, secret-free Provider identity and its fingerprint."""
    environment = environment or os.environ
    capabilities = _dict(provider.capabilities())
    safe_parameters = {
        name: str(environment[name])
        for name in (
            "MODEL_PROVIDER", "MODEL_NAME", "MODEL_TEMPERATURE", "MODEL_TOP_P",
            "MODEL_MAX_TOKENS", "MODEL_REASONING_EFFORT", "MODEL_THINKING",
        )
        if environment.get(name) not in {None, ""}
    }
    safe_parameters["MODEL_BASE_URL"] = _safe_base_url(
        environment.get("MODEL_BASE_URL") or getattr(provider, "base_url", "")
    )
    public = {
        "provider": str(capabilities.get("provider") or "unknown"),
        "transport": str(capabilities.get("transport") or "unknown"),
        "model": str(capabilities.get("model") or getattr(provider, "model_name", "unknown")),
        "modelFamily": str(capabilities.get("modelFamily") or "unknown"),
        "baseUrl": safe_parameters.get("MODEL_BASE_URL", ""),
        "parameters": safe_parameters,
        "credentialConfigured": bool(getattr(provider, "api_key", "")) or
                                str(capabilities.get("authentication") or "") == "local_login",
        "capabilities": {
            key: capabilities.get(key)
            for key in ("supportsThinking", "supportsJsonMode", "supportedLevels", "defaultLevel", "maxTokens")
        },
    }
    fingerprint = hashlib.sha256(
        json.dumps(public, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()
    return {**public, "fingerprint": fingerprint}


def _hash_directory(directory: Path) -> str:
    digest = hashlib.sha256()
    if not directory.exists():
        return digest.hexdigest()
    for path in sorted(item for item in directory.rglob("*") if item.is_file()):
        relative_path = path.relative_to(directory)
        if "__pycache__" in relative_path.parts or path.suffix.lower() in {".pyc", ".pyo"}:
            continue
        relative = relative_path.as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def freeze_runtime_configuration(suite: Path, workflow_digest: str, provider: dict[str, Any]) -> dict[str, Any]:
    """Snapshot prompt registries so every repeated Run sees identical policy."""
    config_root = suite / "config-snapshot"
    agent_directory = config_root / "agents"
    skill_directory = config_root / "skills"
    shutil.copytree(BACKEND / "agents", agent_directory)
    shutil.copytree(BACKEND / "skills", skill_directory)
    agent_digest = _hash_directory(agent_directory)
    skill_digest = _hash_directory(skill_directory)
    identity = {
        "workflow_sha256": workflow_digest,
        "agents_sha256": agent_digest,
        "skills_sha256": skill_digest,
        "runtime_code_sha256": _hash_directory(BACKEND / "app"),
        "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "provider_fingerprint": provider["fingerprint"],
    }
    config_fingerprint = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {
        **identity,
        "fingerprint": config_fingerprint,
        "agent_directory": str(agent_directory),
        "skill_directory": str(skill_directory),
    }


def _number(value: Any) -> int:
    try:
        return max(0, int(value))
    except (ValueError, TypeError, OverflowError):
        return 0


def _decimal(value: Any) -> float:
    try:
        number = float(value)
        return max(0.0, number) if math.isfinite(number) else 0.0
    except (ValueError, TypeError, OverflowError):
        return 0.0


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def redact(value: Any) -> Any:
    """Keep secrets out of JSON and captured worker console output."""
    if isinstance(value, dict):
        return {key: "[REDACTED]" if re.search(r"api.?key|secret|password|authorization|cookie", str(key), re.I)
                else redact(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    if isinstance(value, str):
        for secret in _SECRET_VALUES:
            value = value.replace(secret, "[REDACTED]")
        for key, secret in os.environ.items():
            if re.search(r"key|secret|password|token|cookie|authorization", key, re.I) and len(secret) >= 6:
                value = value.replace(secret, "[REDACTED]")
        value = re.sub(r"(?i)(bearer\s+)[\w.\-/+=]+", r"\1[REDACTED]", value)
    return value


def write_json(path: Path, value: Any) -> None:
    temporary = path.with_name(path.name + f".{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(redact(value), ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    for attempt in range(8):
        try:
            temporary.replace(path)
            return
        except PermissionError as exc:
            # Windows readers and virus scanners can briefly deny replacement
            # even though the target is an isolated regression snapshot.
            if getattr(exc, "winerror", None) not in {5, 32} or attempt == 7:
                raise
            time.sleep(min(0.02 * (2 ** attempt), 0.5))


def token_metrics(run: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, Any]:
    usage = _dict(run.get("token_usage"))
    if "input_tokens" in usage and "output_tokens" in usage:
        incoming, outgoing = _number(usage["input_tokens"]), _number(usage["output_tokens"])
        source = "repository_attempt_totals"
    else:
        # Terminal events are a ledger: recovered Step rows overwrite earlier
        # attempts. Use rows only for steps that have no ledger entries.
        ledger: dict[str, tuple[int, int]] = {}
        seen = set()
        for event in events:
            if event.get("id") is not None:
                if event["id"] in seen:
                    continue
                seen.add(event["id"])
            payload = _dict(event.get("payload"))
            tokens = _dict(payload.get("tokens"))
            step_id = payload.get("stepId")
            if event.get("type") not in {"step.completed", "step.failed"} or not step_id or not tokens:
                continue
            previous = ledger.get(str(step_id), (0, 0))
            ledger[str(step_id)] = (previous[0] + _number(tokens.get("input")),
                                    previous[1] + _number(tokens.get("output")))
        for step in run.get("steps", []):
            ledger.setdefault(str(step.get("step_id")), (_number(step.get("input_tokens")),
                                                        _number(step.get("output_tokens"))))
        incoming = sum(item[0] for item in ledger.values())
        outgoing = sum(item[1] for item in ledger.values())
        source = "terminal_events_and_uncovered_steps"
    return {"input_tokens": incoming, "output_tokens": outgoing, "total_tokens": incoming + outgoing,
            "source": source, "available": bool(usage or events or run.get("steps"))}


def attempt_metrics(run: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, Any]:
    """Count corrective work across node executions, not overwritten Step rows.

    Artifact repairing/continuing events survive failed generation and hard
    cancellation, when no ArtifactGenerationResult/terminal summary exists.
    Reconcile those starts with summaries using max per execution, never add
    both. Core retryCount includes artifact repairs and ordinary continuations;
    subtract those before counting retries. Validation repairs count rounds,
    not files, and repair_skipped is not an attempted repair.
    """
    attempts: dict[tuple[int, str, int], list[dict[str, Any]]] = {}
    serials: dict[str, int] = {}
    cycle = 0
    pending_recovery = False
    closed = set()
    seen = set()
    for event in events:
        if event.get("id") is not None:
            if event["id"] in seen:
                continue
            seen.add(event["id"])
        kind = str(event.get("type", ""))
        payload = _dict(event.get("payload"))
        if kind == "workflow.retrying":
            cycle = max(cycle + 1, _number(payload.get("recoveryCount")))
            pending_recovery = True
        elif kind == "workflow.recovered":
            cycle = max(cycle if pending_recovery else cycle + 1, _number(payload.get("recoveryCount")))
            pending_recovery = False
        if not kind.startswith("step."):
            continue
        step = str(payload.get("validationStepId", payload.get("stepId", "")))
        if kind == "step.started":
            serials[step] = serials.get(step, 0) + 1
        key = (cycle, step, serials.get(step, 0))
        if key in closed:
            serials[step] = serials.get(step, 0) + 1
            key = (cycle, step, serials[step])
        attempts.setdefault(key, []).append(event)
        if kind in {"step.completed", "step.failed", "step.skipped"}:
            closed.add(key)

    rows = {str(step.get("step_id")): step for step in run.get("steps", [])}
    latest = {key[1]: key for key in attempts}
    # Rows are only a fallback for the latest execution; an earlier execution
    # must retain its terminal ledger counters even after the row is reset.
    for step in rows:
        if step not in latest:
            key = (cycle, step, 0)
            attempts[key] = []
            latest[step] = key

    generation_repairs = validation_repairs = retries = continuations = fallbacks = 0
    retry_ambiguous = False
    terminal_steps = set()
    validation_kinds = {"step.validation_repairing", "step.validation_repair_failed",
                        "step.validation_repaired", "step.validation_owner_reexecuting"}
    for key, ledger in attempts.items():
        gen_starts = gen_summary = gen_cont_starts = gen_cont_summary = 0
        retry_starts = plain_cont_starts = raw_retry = 0
        rounds = set()
        artifact_mode = False
        for item in ledger:
            kind = item.get("type")
            payload = _dict(item.get("payload"))
            if kind == "step.artifact_repairing":
                gen_starts += 1
            elif kind == "step.artifact_continuing":
                gen_cont_starts += 1
            elif kind == "step.retrying":
                retry_starts += 1
            elif kind == "step.continuing":
                plain_cont_starts += 1
            elif kind == "step.skipped":
                terminal_steps.add(key[1])
            if kind in validation_kinds and _number(payload.get("repairAttempt")):
                rounds.add(_number(payload["repairAttempt"]))
            if kind in {"step.completed", "step.failed"}:
                terminal_steps.add(key[1])
                runtime = _dict(payload.get("runtime"))
                artifact_mode = artifact_mode or runtime.get("generationMode") == "artifacts"
                gen_summary = max(gen_summary, _number(runtime.get("artifactRepairCount", payload.get("artifactRepairCount"))))
                gen_cont_summary = max(gen_cont_summary, _number(runtime.get("artifactContinuationCount", payload.get("artifactContinuationCount"))))
                raw_retry = max(raw_retry, _number(payload.get("retryCount")))
                fallbacks += payload.get("fallback") is True
        if latest.get(key[1]) == key and key[1] in rows:
            row_retry = _number(rows[key[1]].get("retry_count"))
            # With no events, row retry_count cannot distinguish retries from
            # artifact repairs. Retain it conservatively and expose uncertainty.
            retry_ambiguous = retry_ambiguous or (not ledger and row_retry > 0)
            raw_retry = max(raw_retry, row_retry)
        gen_count = max(gen_starts, gen_summary)
        generation_repairs += gen_count
        validation_repairs += len(rounds)
        continuations += plain_cont_starts + max(gen_cont_starts, gen_cont_summary)
        # Successful artifact generation stores its repair count in retryCount.
        # A failed generation has only start events; subtracting clamps at zero.
        retries += max(retry_starts, raw_retry - gen_count - plain_cont_starts)
        retry_ambiguous = retry_ambiguous or (artifact_mode and raw_retry > gen_count and not retry_starts)

    required_steps = {step for step, row in rows.items() if row.get("agent_id")}
    available = bool(terminal_steps) and required_steps <= terminal_steps and not retry_ambiguous
    return {"repair_count": generation_repairs + validation_repairs,
            "generation_repair_count": generation_repairs, "validation_repair_count": validation_repairs,
            "retry_count": retries, "continuation_count": continuations, "fallback_count": fallbacks,
            "recovery_count": max(cycle, _number(run.get("recovery_count")),
                                  _number(_dict(run.get("state")).get("recovery_count"))),
            "attempt_metrics_available": available, "retry_count_ambiguous": retry_ambiguous}


def repair_metrics(events: list[dict[str, Any]]) -> int:
    return attempt_metrics({}, events)["repair_count"]


def _first_pass(row: dict[str, Any]) -> bool | None:
    if row.get("deliverable") is not True:
        return False
    if any(_number(row.get(field)) for field in
           ("repair_count", "retry_count", "continuation_count", "fallback_count", "recovery_count")):
        return False
    return True if row.get("attempt_metrics_available") is True else None


def _gate_passed(gate: Any) -> bool:
    gate = _dict(gate)
    checks = gate.get("checks")
    if not isinstance(checks, list) or not checks:
        return False
    if any(not isinstance(check, dict) or check.get("status") != "passed" for check in checks):
        return False
    return gate.get("status") == "passed" and gate.get("deliverable", True) is not False


def stack_matches(case: Case, run: dict[str, Any]) -> bool:
    context = _dict(_dict(run.get("state")).get("context"))
    files = context.get("__artifact_files__", [])
    names = [str(item.get("path", item.get("name", ""))).replace("\\", "/").lower()
             for item in files if isinstance(item, dict)]
    names += [str(item.get("name", "")).replace("\\", "/").lower()
              for item in run.get("artifacts", []) if isinstance(item, dict)]
    spring = any(name.endswith("pom.xml") for name in names) and any(name.endswith(".java") for name in names)
    vue = any(name.endswith(".vue") for name in names) and any(name.endswith("package.json") for name in names)
    html = any(name.endswith(".html") for name in names)
    if case.expected_stack == "static_html":
        return html and not spring and not vue
    if case.expected_stack == "springboot_vue":
        return spring and vue
    return spring and html and not vue


def measure(case: Case, run: dict[str, Any], events: list[dict[str, Any]], duration: float,
            *, outcome: str | None = None, error: str | None = None) -> dict[str, Any]:
    context = _dict(_dict(run.get("state")).get("context"))
    # Consume future core delivery fields without importing a speculative API.
    contract = run.get("delivery_contract", context.get("delivery_contract"))
    gate = run.get("delivery_gate", context.get("delivery_gate"))
    gate_source = "delivery_gate"
    if gate is None:
        if contract is not None:
            gate = {"status": "blocked", "checks": [], "summary": "Contract delivery gate evidence missing"}
        else:
            gate = context.get("artifact_validation")
            gate_source = "artifact_validation"
    status = outcome or str(run.get("status", "FAILED"))
    success = status == "SUCCESS"
    matched = stack_matches(case, run)
    deliverable = success and matched and _gate_passed(gate)
    corrections = attempt_metrics(run, events)
    parameter_profile, parameter_fingerprint = resolved_parameter_profile(events)
    summary = error or run.get("error_message")
    if not summary and not deliverable:
        summary = (_dict(gate).get("summary") or
                   ("Missing delivery checks: " + ", ".join(map(str, _dict(gate)["missing"]))
                    if _dict(gate).get("missing") else "No complete deterministic delivery gate evidence")) if not _gate_passed(gate) else "Requested stack missing from artifacts"
    return {"case_id": case.id, "expected_stack": case.expected_stack, "run_id": run.get("id"),
            "status": status, "executor_status": run.get("status"), "success": success,
            "deliverable": deliverable, "stack_matches": matched, "failure_summary": redact(str(summary)) if summary else None,
            "duration_seconds": round(max(0, duration), 3), "active_duration_ms": run.get("active_duration_ms"),
            "tokens": token_metrics(run, events), **corrections,
            "coding_loop": coding_loop_metrics(events),
            "phase_durations": phase_metrics(events),
            "resolved_parameters": parameter_profile,
            "resolved_parameter_fingerprint": parameter_fingerprint,
            "first_pass": _first_pass({"deliverable": deliverable, **corrections}),
            "delivery_contract": contract, "delivery_gate": gate, "gate_source": gate_source,
            "difficulty": case.difficulty or None, "cohort": case.cohort}


def coding_loop_metrics(events: list[dict[str, Any]]) -> dict[str, int]:
    counts = {
        "iterations": 0, "actions": 0, "action_failures": 0,
        "repair_candidates": 0, "repair_interruptions": 0, "no_progress_breakers": 0,
    }
    for event in events:
        kind = str(event.get("type") or "")
        payload = _dict(event.get("payload"))
        if kind == "coding_loop.iteration_started":
            counts["iterations"] += 1
        elif kind == "coding_loop.action_started":
            counts["actions"] += 1
        elif kind == "coding_loop.action_completed" and payload.get("success") is False:
            counts["action_failures"] += 1
        elif kind == "coding_loop.repair_candidate":
            counts["repair_candidates"] += 1
        elif kind == "coding_loop.repair_interrupted":
            counts["repair_interruptions"] += 1
        elif kind == "coding_loop.no_progress":
            counts["no_progress_breakers"] += 1
    return counts


def phase_metrics(events: list[dict[str, Any]]) -> dict[str, dict[str, dict[str, int]]]:
    """Collect measured Agent and deterministic-validation durations from events."""
    steps: dict[str, dict[str, int]] = {}
    validation: dict[str, dict[str, int]] = {}
    seen = set()
    for event in events:
        if event.get("id") is not None:
            if event["id"] in seen:
                continue
            seen.add(event["id"])
        kind = str(event.get("type") or "")
        payload = _dict(event.get("payload"))
        duration = _number(payload.get("durationMs"))
        if kind in {"step.completed", "step.failed"} and payload.get("stepId"):
            name = str(payload["stepId"])
            current = steps.setdefault(name, {"attempts": 0, "total_ms": 0})
            current["attempts"] += 1
            current["total_ms"] += duration
        elif kind == "step.validation_stage_completed" and payload.get("stage"):
            name = str(payload["stage"])
            current = validation.setdefault(name, {"attempts": 0, "total_ms": 0})
            current["attempts"] += 1
            current["total_ms"] += duration
    return {"agents": steps, "validation_stages": validation}


def resolved_parameter_profile(events: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], str | None]:
    """Fingerprint actual non-secret per-step settings, including retry attempts."""
    rows = []
    safe_request_keys = {
        "provider", "transport", "model", "execution", "sandbox", "ephemeral",
        "requested_max_tokens", "max_tokens", "token_control", "structured_output",
        "enable_thinking", "reasoning_effort", "thinking", "temperature", "top_p",
        "response_format",
    }
    seen = set()
    for event in events:
        if event.get("id") is not None:
            if event["id"] in seen:
                continue
            seen.add(event["id"])
        if str(event.get("type") or "") not in {"step.completed", "step.failed"}:
            continue
        payload = _dict(event.get("payload"))
        runtime = _dict(payload.get("runtime"))
        provider = _dict(payload.get("provider"))
        request = _dict(provider.get("request_parameters"))
        request_parameters = {key: request[key] for key in sorted(safe_request_keys) if key in request}
        rows.append({
            "step_id": str(payload.get("stepId") or "unknown"),
            "budget": {
                key: runtime[key]
                for key in ("configuredMaxTokens", "recommendedMaxTokens", "maxTokens", "retry", "budgetMode",
                            "requestedThinking", "effectiveThinking", "generationMode")
                if key in runtime
            },
            "provider": {key: provider[key] for key in ("provider", "transport", "model") if key in provider},
            "request_parameters": request_parameters,
        })
    if not rows:
        return [], None
    serialized = json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return rows, hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def aggregate(results: list[dict[str, Any]]) -> dict[str, Any]:
    def totals(rows: list[dict[str, Any]]) -> dict[str, Any]:
        count = len(rows)
        successes = sum(row.get("success") is True for row in rows)
        delivered = sum(row.get("deliverable") is True for row in rows)
        first_passes = sum(_first_pass(row) is True for row in rows)
        return {"runs": count, "successes": successes, "deliverables": delivered,
                "success_rate": successes / count if count else 0.0,
                "deliverable_rate": delivered / count if count else 0.0,
                "duration_seconds": round(sum(row.get("duration_seconds", 0) for row in rows), 3),
                "total_tokens": sum(_number(_dict(row.get("tokens")).get("total_tokens")) for row in rows),
                "first_passes": first_passes, "first_pass_rate": first_passes / count if count else 0.0,
                "first_pass_unknown_runs": sum(_first_pass(row) is None for row in rows),
                **{field: sum(_number(row.get(field)) for row in rows) for field in
                   ("repair_count", "generation_repair_count", "validation_repair_count", "retry_count",
                    "continuation_count", "fallback_count", "recovery_count")},
                "coding_loop_iterations": sum(_number(_dict(row.get("coding_loop")).get("iterations")) for row in rows),
                "coding_loop_actions": sum(_number(_dict(row.get("coding_loop")).get("actions")) for row in rows),
                "coding_loop_repair_candidates": sum(_number(_dict(row.get("coding_loop")).get("repair_candidates")) for row in rows)}
    def grouped_phase_durations(rows: list[dict[str, Any]], group: str) -> dict[str, dict[str, int]]:
        values: dict[str, list[int]] = {}
        for row in rows:
            phases = _dict(row.get("phase_durations"))
            for name, item in _dict(phases.get(group)).items():
                attempts = _number(_dict(item).get("attempts"))
                if attempts:
                    values.setdefault(str(name), []).append(_number(_dict(item).get("total_ms")) // attempts)
        return {
            name: {
                "samples": len(samples),
                "mean_ms": round(statistics.fmean(samples)),
                "median_ms": round(statistics.median(samples)),
                "p95_ms": sorted(samples)[max(0, math.ceil(len(samples) * 0.95) - 1)],
            }
            for name, samples in sorted(values.items())
        }

    def consistency(rows: list[dict[str, Any]]) -> dict[str, Any]:
        def unique(field: str) -> set[str]:
            return {str(row[field]) for row in rows if row.get(field)}
        provider = unique("provider_fingerprint")
        config = unique("configuration_fingerprint")
        parameters = unique("resolved_parameter_fingerprint")
        return {
            "repetitions": len(rows),
            "provider_consistent": len(provider) == 1 if rows and all(row.get("provider_fingerprint") for row in rows) else None,
            "configuration_consistent": len(config) == 1 if rows and all(row.get("configuration_fingerprint") for row in rows) else None,
            "resolved_parameters_consistent": len(parameters) == 1 if rows and all(row.get("resolved_parameter_fingerprint") for row in rows) else None,
        }

    def totals_with_phases(rows: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            **totals(rows),
            "agent_phase_duration_ms": grouped_phase_durations(rows, "agents"),
            "validation_phase_duration_ms": grouped_phase_durations(rows, "validation_stages"),
            **consistency(rows),
        }

    return {
        **totals_with_phases(results),
        "by_case": {case_id: totals_with_phases([row for row in results if row["case_id"] == case_id])
                    for case_id in sorted({row["case_id"] for row in results})},
        "by_cohort": {cohort: totals_with_phases([row for row in results if row.get("cohort", "known") == cohort])
                      for cohort in sorted({str(row.get("cohort", "known")) for row in results})},
    }


def render_markdown_report(report: dict[str, Any]) -> str:
    summary = _dict(report.get("summary"))
    lines = [
        "# Agent-Team Coding Agent 难度递增回归报告",
        "",
        f"- 模式：{report.get('mode', 'unknown')}",
        f"- 工作流 SHA-256：{report.get('workflow_sha256', 'unknown')}",
        f"- Provider / 模型：{_dict(report.get('provider')).get('provider', 'unknown')} / {_dict(report.get('provider')).get('model', 'unknown')}",
        f"- Provider 指纹：{_dict(report.get('provider')).get('fingerprint', 'unknown')}",
        f"- 运行配置指纹：{_dict(report.get('configuration')).get('fingerprint', 'unknown')}",
        f"- 完成数：{summary.get('runs', 0)}",
        f"- 可交付：{summary.get('deliverables', 0)}/{summary.get('runs', 0)} ({_decimal(summary.get('deliverable_rate')):.1%})",
        f"- 首轮可交付：{summary.get('first_passes', 0)}/{summary.get('runs', 0)} ({_decimal(summary.get('first_pass_rate')):.1%})",
        f"- 总耗时：{_decimal(summary.get('duration_seconds')):.1f} 秒；总 Token：{_number(summary.get('total_tokens'))}",
        f"- 编码循环：{_number(summary.get('coding_loop_iterations'))} 轮、{_number(summary.get('coding_loop_actions'))} 个工具动作、{_number(summary.get('coding_loop_repair_candidates'))} 个修复候选",
        "",
        "| 测试集 | 难度 | 场景 | 轮次 | 状态 | 可交付 | 首轮通过 | 修复/重试 | 耗时 | Token | 主要失败原因 |",
        "|---|---|---|---:|---|---:|---:|---:|---:|---:|---|",
    ]
    for row in report.get("results", []):
        if not isinstance(row, dict):
            continue
        repairs = _number(row.get("repair_count")) + _number(row.get("retry_count"))
        reason = str(row.get("failure_summary") or "—").replace("|", "\\|").replace("\r", " ").replace("\n", " ")
        if len(reason) > 300:
            reason = reason[:297] + "…"
        tokens = _number(_dict(row.get("tokens")).get("total_tokens"))
        first_value = row.get("first_pass")
        first_pass = "是" if first_value is True else "否" if first_value is False else "未知"
        delivered = "是" if row.get("deliverable") else "否"
        lines.append(
            f"| {row.get('cohort', 'known')} | {row.get('difficulty') or '—'} | {row.get('case_id', '')} "
            f"| {_number(row.get('repetition'))} | {row.get('status', '')} "
            f"| {delivered} | {first_pass} | {repairs} | {_decimal(row.get('duration_seconds')):.1f}s | {tokens} | {reason} |"
        )
    summary = _dict(report.get("summary"))
    lines.extend(["", "## 重复运行稳定性", "", "| 场景 | 轮数 | Provider 一致 | 配置一致 | 实际参数一致 | 可交付率 | 首轮通过率 |",
                  "|---|---:|---|---|---|---:|---:|"])
    for case_id, values in _dict(summary.get("by_case")).items():
        def consistency_label(key: str) -> str:
            return "是" if values.get(key) is True else "否" if values.get(key) is False else "证据不足"
        lines.append(
            f"| {case_id} | {_number(values.get('repetitions'))} | {consistency_label('provider_consistent')} "
            f"| {consistency_label('configuration_consistent')} | {consistency_label('resolved_parameters_consistent')} "
            f"| {_decimal(values.get('deliverable_rate')):.1%} | {_decimal(values.get('first_pass_rate')):.1%} |"
        )
    lines.extend(["", "## 阶段耗时（中位数 / P95）", "", "| 类别 | 阶段 | 中位数 | P95 | 样本数 |", "|---|---|---:|---:|---:|"])
    for group_key, label in (("agent_phase_duration_ms", "Agent"), ("validation_phase_duration_ms", "验证")):
        for phase, timing in _dict(summary.get(group_key)).items():
            lines.append(
                f"| {label} | {phase} | {_number(timing.get('median_ms'))}ms "
                f"| {_number(timing.get('p95_ms'))}ms | {_number(timing.get('samples'))} |"
            )
    lines.extend([
        "",
        "## 解释",
        "",
        "可交付要求工作流成功、技术栈匹配，并且适用的确定性成果物/集成 Gate 有通过证据；Agent 自报完成不计为通过。",
        "重复测试的 Provider 与工作流/Agent/Skill 配置指纹必须一致；参数指纹用于揭示相同需求的运行时预算或思考强度是否漂移。每阶段耗时来自持久化终态事件，人工审批不计入 Agent 时长。",
        "",
    ])
    return "\n".join(lines)


async def drive_run(executor: Any, repository: Any, run_id: str, workflow: Any, inputs: dict[str, Any],
                    *, timeout: float, auto_approve: bool = False, max_retries: int = 0,
                    clarification_answers: dict[str, Any] | None = None,
                    cancel_requested: Callable[[], bool] = lambda: False,
                    checkpoint: Callable[[], None] = lambda: None, poll: float = 0.1) -> str:
    """Drive public executor APIs under one deadline; no production policy patches.

    Recovery requires explicit retryable evidence from the core. Arbitrary
    failures/validation errors must not trigger an expensive fresh model retry.
    The subprocess supervisor enforces the deadline even if cancellation hangs.
    """
    if not math.isfinite(timeout) or timeout <= 0 or not 0 <= max_retries <= 100:
        raise ValueError("timeout must be finite and positive; max_retries must be between 0 and 100")
    deadline = time.monotonic() + timeout
    task = asyncio.create_task(executor.start(run_id, workflow, inputs))
    approved = None
    clarified = None
    retries = 0
    completed = False
    try:
        while True:
            checkpoint()
            if cancel_requested():
                return "CANCELLED"
            if time.monotonic() >= deadline:
                return "TIMEOUT"
            if task.done():
                task.result()
            run = repository.get_run(run_id) or {}
            status = run.get("status")
            if status in TERMINAL:
                # A terminal database write can precede asynchronous graph
                # cleanup. Do not stop it or recover until the executor releases
                # this workflow (including approval's background resume task).
                if workflow is not None and hasattr(executor, "is_active") and executor.is_active(workflow.id):
                    await asyncio.sleep(min(poll, max(0, deadline - time.monotonic())))
                    continue
                if status == "FAILED" and run.get("retryable") is True and retries < max_retries:
                    retries += 1
                    task = asyncio.create_task(executor.retry(run_id, workflow))
                    approved = None
                    await asyncio.sleep(min(poll, max(0, deadline - time.monotonic())))
                    continue
                completed = True
                return str(status)
            if status == "WAITING_APPROVAL":
                waiting = _dict(run.get("state")).get("waiting_step_id")
                waiting = waiting or next((step.get("step_id") for step in run.get("steps", [])
                                           if step.get("status") == "WAITING_APPROVAL"), "approval")
                if auto_approve and waiting != approved:
                    await executor.approve(run_id, "approve")
                    approved = waiting
                    if hasattr(executor, "wait_for_control_resolution"):
                        try:
                            await asyncio.wait_for(
                                executor.wait_for_control_resolution(run_id),
                                timeout=max(0.01, deadline - time.monotonic()),
                            )
                        except asyncio.TimeoutError:
                            return "TIMEOUT"
            elif status == "WAITING_CLARIFICATION":
                request = _dict(run.get("clarification"))
                fields = {str(field) for field in request.get("unresolved_fields") or []}
                answers = _dict(clarification_answers)
                if not fields or not fields.issubset(answers):
                    return "NEEDS_INPUT"
                request_id = str(request.get("request_id") or "")
                if request_id and request_id != clarified:
                    await executor.clarify(
                        run_id,
                        {**{field: answers[field] for field in fields}, "request_id": request_id},
                    )
                    clarified = request_id
                    if hasattr(executor, "wait_for_control_resolution"):
                        try:
                            await asyncio.wait_for(
                                executor.wait_for_control_resolution(run_id),
                                timeout=max(0.01, deadline - time.monotonic()),
                            )
                        except asyncio.TimeoutError:
                            return "TIMEOUT"
            else:
                approved = None
                clarified = None
            await asyncio.sleep(min(poll, max(0, deadline - time.monotonic())))
    finally:
        if completed:
            cleanup_tasks = {task}
        else:
            task.cancel()
            cleanup_tasks = {task, asyncio.create_task(executor.stop(run_id))}
        # asyncio.wait has a bounded cancellation wait, unlike wait_for on an
        # awaitable that suppresses CancelledError. The parent kills stragglers.
        done, _ = await asyncio.wait(cleanup_tasks, timeout=0.5)
        for finished in done:
            try:
                finished.result()
            except (Exception, asyncio.CancelledError):
                pass


class ProcessTree:
    """POSIX session or Windows kill-on-close Job; workers wait for assignment."""

    def __init__(self, process: subprocess.Popen[Any]) -> None:
        self.process = process
        self.job = None
        if os.name != "nt":
            return
        import ctypes
        from ctypes import wintypes

        class Basic(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class IO(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in ("ReadOperationCount", "WriteOperationCount",
                       "OtherOperationCount", "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class Extended(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", Basic), ("IoInfo", IO), ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                        ("PeakJobMemoryUsed", ctypes.c_size_t)]

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel = kernel
        self.job = kernel.CreateJobObjectW(None, None)
        if not self.job:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = Extended()
        limits.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel.SetInformationJobObject(self.job, 9, ctypes.byref(limits), ctypes.sizeof(limits)) or not kernel.AssignProcessToJobObject(self.job, int(process._handle)):
            failure = ctypes.WinError(ctypes.get_last_error())
            kernel.CloseHandle(self.job)
            self.job = None
            raise failure

    def close(self) -> None:
        if os.name == "nt":
            if self.job:
                self.kernel.CloseHandle(self.job)
                self.job = None
        else:
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait(timeout=5)


def supervise(command: list[str], directory: Path, *, timeout: float, grace: float = 2,
              cancel_requested: Callable[[], bool] = lambda: False,
              environment: Mapping[str, str] | None = None,
              process_factory: Callable[..., Any] = subprocess.Popen,
              tree_factory: Callable[..., Any] = ProcessTree) -> dict[str, Any]:
    start = time.monotonic()
    outcome = None
    process = None
    tree = None
    with (directory / "worker.log").open("wb") as log:
        try:
            process = process_factory(command, cwd=directory, stdout=log, stderr=subprocess.STDOUT,
                                      start_new_session=os.name != "nt", env=dict(environment) if environment else None)
            tree = tree_factory(process)
            (directory / "release").touch()  # no model work before containment
            while process.poll() is None:
                if cancel_requested() or time.monotonic() - start >= timeout:
                    outcome = "CANCELLED" if cancel_requested() else "TIMEOUT"
                    (directory / "cancel").touch()
                    try:
                        process.wait(timeout=grace)
                    except subprocess.TimeoutExpired:
                        pass
                    break
                time.sleep(0.05)
        except KeyboardInterrupt:
            outcome = "CANCELLED"
            (directory / "cancel").touch()
            if process:
                try:
                    process.wait(timeout=grace)
                except subprocess.TimeoutExpired:
                    pass
        except Exception as exc:
            outcome = "FAILED"
            write_json(directory / "supervisor-error.json", {"error": f"{type(exc).__name__}: {exc}"})
        finally:
            if tree:
                tree.close()  # also kills descendants after a normal worker exit
            elif process:
                if os.name == "nt":
                    # Assignment failed before release. Remove the rtk wrapper
                    # and its blocked Python child using the Windows tree API.
                    subprocess.run(["rtk", "proxy", "taskkill", "/PID", str(process.pid), "/T", "/F"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
                process.kill()
                process.wait(timeout=5)
    # Scrub captured library output, including keys loaded from backend/.env.
    log_path = directory / "worker.log"
    log_path.write_text(redact(log_path.read_text(encoding="utf-8", errors="replace")), encoding="utf-8")
    return {"outcome": outcome, "returncode": process.returncode if process else None,
            "duration": time.monotonic() - start}


def _imports() -> None:
    if str(BACKEND) not in sys.path:
        sys.path.insert(0, str(BACKEND))


def real_provider(environment: Mapping[str, str] | None = None) -> Any:
    from dotenv import load_dotenv
    from app.llm.factory import ProviderFactory
    from app.llm.mock import MockProvider

    if environment is None:
        load_dotenv(BACKEND / ".env", override=False)
        provider = ProviderFactory.create_from_env()
    else:
        provider = ProviderFactory.create(
            str(environment.get("MODEL_PROVIDER", "qwen")),
            str(environment.get("MODEL_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")),
            str(environment.get("MODEL_API_KEY") or ""),
            str(environment.get("MODEL_NAME", "qwen-plus")),
        )
    if isinstance(provider, MockProvider):
        raise ValueError("--live requires a real Provider; MODEL_* configuration resolved to MockProvider")
    return provider


def collect_site(directory: Path, repository: Any, artifacts: Any, run_id: str,
                 workflow_id: str) -> dict[str, Any]:
    """Retain partial sources; use an available core gate without inventing proof."""
    run = repository.get_run(run_id) or {"id": run_id}
    context = _dict(_dict(run.get("state")).get("context"))
    artifacts.materialize(run_id, workflow_id, context, run.get("final_report"))
    archive = None
    if repository.list_artifacts(run_id):
        archive = artifacts.create_archive(run_id, strict=bool(context.get("delivery_contract")))
    run = repository.get_run(run_id) or run
    persisted_gate = run.get("delivery_gate", context.get("delivery_gate"))
    if persisted_gate is not None:
        # Final core proof may include human approval and other checks absent
        # from artifact_validation. Preserve it instead of weakening/replacing it.
        run["delivery_gate"] = persisted_gate
    elif context.get("delivery_contract") is not None:
        try:
            module = importlib.import_module("app.workflow.delivery_gate")
        except ModuleNotFoundError as exc:
            if exc.name != "app.workflow.delivery_gate":
                raise
            # A present contract with no compatible evaluator cannot fall back
            # to the weaker legacy validator and claim contract delivery.
            run["delivery_gate"] = {"status": "blocked", "checks": [],
                                    "summary": "Core delivery gate evaluator unavailable"}
        else:
            evaluate = getattr(module, "evaluate_delivery", None)
            if callable(evaluate):
                run["delivery_gate"] = evaluate(context, context.get("artifact_validation"), archive)
            elif "delivery_gate" not in run and "delivery_gate" not in context:
                run["delivery_gate"] = {"status": "blocked", "checks": [],
                                        "summary": "Core delivery gate evaluator unavailable"}
    return {"run": run, "events": repository.list_events(run_id)}


def salvage(directory: Path, spec: dict[str, Any]) -> dict[str, Any]:
    """Parent-side recovery after hard kill: no executor or Provider creation."""
    evidence = {}
    if (directory / "evidence.json").exists():
        evidence = json.loads((directory / "evidence.json").read_text(encoding="utf-8"))
    if (directory / "run.sqlite3").exists():
        from app.repositories.sqlite import SQLiteRepository
        from app.services.artifacts import ArtifactService

        repository = SQLiteRepository(directory / "run.sqlite3")
        try:
            evidence = collect_site(directory, repository, ArtifactService(repository, directory / "workspace"),
                                    spec["run_id"], spec["workflow_id"])
            write_json(directory / "evidence.json", evidence)
        except Exception as exc:
            write_json(directory / "salvage-error.json", {"error": f"{type(exc).__name__}: {exc}"})
        finally:
            repository._connection.close()
    return evidence


async def execute_worker(directory: Path, spec: dict[str, Any]) -> None:
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
    from app.agents.registry import AgentRegistry
    from app.repositories.sqlite import SQLiteRepository
    from app.services.artifacts import ArtifactService
    from app.skills.registry import SkillRegistry
    from app.workflow.dag import build_dag
    from app.workflow.events import WorkflowEventBus
    from app.workflow.executor import WorkflowExecutor
    from app.workflow.models import utc_now
    from app.workflow.parser import parse_workflow_file

    if spec.get("live") is not True:
        raise ValueError("Worker refuses model execution without explicit live flag")
    provider = real_provider()
    expected_provider = _dict(spec.get("provider_snapshot"))
    actual_provider = provider_snapshot(provider)
    if expected_provider.get("fingerprint") and actual_provider["fingerprint"] != expected_provider["fingerprint"]:
        raise RuntimeError("Resolved Provider/model/parameters differ from the frozen evaluation configuration")
    expected_runtime_hash = str(_dict(spec.get("configuration")).get("runtime_code_sha256") or "")
    if expected_runtime_hash and _hash_directory(BACKEND / "app") != expected_runtime_hash:
        raise RuntimeError("Runtime source changed after the evaluation snapshot was frozen")
    expected_harness_hash = str(_dict(spec.get("configuration")).get("harness_sha256") or "")
    if expected_harness_hash and hashlib.sha256(Path(__file__).read_bytes()).hexdigest() != expected_harness_hash:
        raise RuntimeError("Regression harness changed after the evaluation snapshot was frozen")
    # Local Provider must also stay within this Run's controlled workspace.
    if hasattr(provider, "working_directory"):
        provider.working_directory = str(directory / "workspace")
    workflow = parse_workflow_file(directory / "workflow.yaml", spec["workflow_id"])
    build_dag(workflow)
    repository = SQLiteRepository(directory / "run.sqlite3")
    artifacts = ArtifactService(repository, directory / "workspace")
    bus = WorkflowEventBus(repository)
    agent_directory = Path(str(spec.get("agent_directory") or BACKEND / "agents"))
    skill_directory = Path(str(spec.get("skill_directory") or BACKEND / "skills"))
    executor = WorkflowExecutor(AgentRegistry.from_directory(agent_directory), provider, bus, repository,
                                artifact_service=artifacts,
                                skill_registry=SkillRegistry.from_directories([skill_directory]))
    run_id = spec["run_id"]
    inputs = {"requirement": spec["case"]["requirement"]}
    repository.create_run(run_id, workflow.id, inputs, "PENDING", utc_now())
    repository.save_workflow_snapshot(run_id, spec["workflow_snapshot"])
    started = time.monotonic()

    def checkpoint() -> None:
        write_json(directory / "evidence.json", {"run": repository.get_run(run_id),
                   "events": repository.list_events(run_id)})

    outcome = None
    error = None
    try:
        async with AsyncExitStack() as stack:
            checkpointer = await stack.enter_async_context(
                AsyncSqliteSaver.from_conn_string(str(directory / "checkpoint.sqlite3")))
            await checkpointer.setup()
            executor.checkpointer = checkpointer
            outcome = await drive_run(executor, repository, run_id, workflow, inputs,
                                     timeout=spec["timeout"], auto_approve=spec["auto_approve"],
                                     max_retries=spec["max_retries"],
                                     clarification_answers=spec["case"].get("clarification_answers"),
                                     cancel_requested=lambda: (directory / "cancel").exists(), checkpoint=checkpoint)
            if outcome == "NEEDS_INPUT":
                request = _dict((repository.get_run(run_id) or {}).get("clarification"))
                error = "Requirement clarification needed: " + ", ".join(map(str, request.get("unresolved_fields") or []))
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        outcome = "FAILED"
    finally:
        try:
            evidence = collect_site(directory, repository, artifacts, run_id, workflow.id)
        except Exception as exc:
            error = f"{error or ''}; failure-site materialization: {type(exc).__name__}: {exc}"
            outcome = "FAILED"
            evidence = {"run": repository.get_run(run_id), "events": repository.list_events(run_id)}
        write_json(directory / "evidence.json", evidence)
        result = measure(Case(**spec["case"]), evidence["run"] or {}, evidence["events"],
                         time.monotonic() - started, outcome=outcome, error=error)
        write_json(directory / "result.json", result)
        repository._connection.close()


def worker(directory: Path) -> int:
    # Barrier closes the spawn/Job-assignment race on Windows. Parent death
    # before release cannot leave a worker doing real work indefinitely.
    deadline = time.monotonic() + 15
    while not (directory / "release").exists():
        if time.monotonic() >= deadline or (directory / "cancel").exists():
            return 2
        time.sleep(0.05)
    _imports()
    spec = json.loads((directory / "spec.json").read_text(encoding="utf-8"))
    try:
        asyncio.run(execute_worker(directory, spec))
    except Exception as exc:
        write_json(directory / "result.json", measure(Case(**spec["case"]), {"id": spec["run_id"]}, [], 0,
                   outcome="FAILED", error=f"{type(exc).__name__}: {exc}"))
        return 1
    return 0


def _positive(value: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("must be finite and positive")
    return number


def _bounded_int(value: str) -> int:
    number = int(value)
    if not 0 <= number <= 100:
        raise argparse.ArgumentTypeError("must be between 0 and 100")
    return number


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--auto-approve", action="store_true", help="approve only isolated test Runs")
    parser.add_argument("--cases", type=Path)
    parser.add_argument("--unseen-cases", type=Path,
                        help="separate holdout JSON list; these requirements are reported as the unseen cohort")
    parser.add_argument("--case-id", action="append", default=[], help="run only matching case ID; repeat to select several")
    parser.add_argument("--repeat", type=_bounded_int, default=1)
    parser.add_argument("--timeout", type=_positive, default=1800,
                        help="maximum wall time for the workflow Run; defaults to 30 minutes for multi-Agent integration Gates")
    parser.add_argument("--finalize-grace", type=_positive, default=45,
                        help="bounded time after workflow deadline for durable result and gate evidence to flush")
    parser.add_argument("--cancel-grace", type=_positive, default=2)
    parser.add_argument("--max-retries", type=_bounded_int, default=0,
                        help="bounded core recovery, only with explicit retryable failure evidence")
    parser.add_argument("--workflow", type=Path, default=BACKEND / "app/workflows/software-development.yaml")
    parser.add_argument("--output", type=Path, default=BACKEND / "data/regression")
    parser.add_argument("--worker", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.worker:
        return worker(args.worker.resolve())
    if args.repeat < 1:
        parser.error("--repeat must be at least 1")
    cases = load_cases(args.cases)
    if args.unseen_cases:
        cases += load_cases(args.unseen_cases, cohort="unseen")
    if len({case.id for case in cases}) != len(cases):
        parser.error("known and unseen test case IDs must be unique")
    if args.case_id:
        selected = set(args.case_id)
        unknown = selected - {case.id for case in cases}
        if unknown:
            parser.error(f"unknown --case-id: {', '.join(sorted(unknown))}")
        cases = tuple(case for case in cases if case.id in selected)
    if not args.live:
        print(json.dumps({"mode": "plan", "live": False, "repeat": args.repeat,
                          "cases": [asdict(case) for case in cases]}, ensure_ascii=False, indent=2))
        return 0
    _imports()
    from app.workflow.dag import build_dag
    from app.workflow.parser import parse_workflow_yaml
    from dotenv import dotenv_values

    dotenv_environment = dotenv_values(BACKEND / ".env")

    for key, secret in dotenv_environment.items():
        if secret and len(secret) >= 6 and re.search(r"key|secret|password|token|cookie|authorization", key, re.I):
            _SECRET_VALUES.add(secret)

    # Resolve the Provider once and pass this private in-memory environment to
    # each worker. Credentials are never copied to spec/report files.
    frozen_environment = dict(os.environ)
    for key, value in dotenv_environment.items():
        if key not in frozen_environment and value is not None:
            frozen_environment[key] = value
    provider_configuration = provider_snapshot(real_provider(frozen_environment), frozen_environment)

    # Read once. Every repeat uses the same current, validated snapshot.
    yaml_text = args.workflow.resolve().read_text(encoding="utf-8")
    workflow = parse_workflow_yaml(yaml_text, args.workflow.stem)
    build_dag(workflow)
    snapshot = json.loads(json.dumps(asdict(workflow)))
    digest = hashlib.sha256(yaml_text.encode("utf-8")).hexdigest()
    suite = args.output.resolve() / (time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8])
    suite.mkdir(parents=True, exist_ok=False)
    write_json(suite / "workflow-snapshot.json", snapshot)
    runtime_configuration = freeze_runtime_configuration(suite, digest, provider_configuration)
    report_configuration = {
        key: value for key, value in runtime_configuration.items()
        if key not in {"agent_directory", "skill_directory"}
    }
    write_json(suite / "experiment.json", {
        "provider": provider_configuration,
        "configuration": report_configuration,
        "repeat": args.repeat,
        "cohorts": sorted({case.cohort for case in cases}),
        "cases": [{"id": case.id, "expected_stack": case.expected_stack, "difficulty": case.difficulty,
                   "cohort": case.cohort} for case in cases],
        "workflow_sha256": digest,
    })
    results: list[dict[str, Any]] = []
    cancelled = False
    rtk = shutil.which("rtk")
    if not rtk:
        raise RuntimeError("rtk is required for subprocess execution")
    for case in cases:
        for repetition in range(1, args.repeat + 1):
            directory = suite / f"{case.id}-{repetition}"
            directory.mkdir()
            (directory / "workspace").mkdir()
            (directory / "workflow.yaml").write_text(yaml_text, encoding="utf-8")
            spec = {"live": True, "case": asdict(case), "run_id": "regression-" + uuid.uuid4().hex,
                    "workflow_id": workflow.id, "workflow_snapshot": snapshot, "timeout": args.timeout,
                    "auto_approve": args.auto_approve, "max_retries": args.max_retries,
                    "provider_snapshot": provider_configuration,
                    "configuration": report_configuration,
                    "configuration_fingerprint": runtime_configuration["fingerprint"],
                    "agent_directory": runtime_configuration["agent_directory"],
                    "skill_directory": runtime_configuration["skill_directory"]}
            write_json(directory / "spec.json", spec)
            process_result = supervise([rtk, "proxy", sys.executable, str(Path(__file__).resolve()),
                                        "--worker", str(directory)],
                                       directory,
                                       # The worker enforces the actual workflow
                                       # deadline; this outer grace is only for
                                       # persisting terminal evidence and cleanup.
                                       timeout=args.timeout + args.finalize_grace,
                                       grace=args.cancel_grace,
                                       environment=frozen_environment,
                                       cancel_requested=lambda: (suite / "cancel").exists())
            evidence = salvage(directory, spec)
            if (directory / "result.json").exists() and not process_result["outcome"]:
                result = json.loads((directory / "result.json").read_text(encoding="utf-8"))
                if process_result["returncode"] != 0:
                    result.update(status="FAILED", success=False, deliverable=False)
            elif process_result["outcome"] == "TIMEOUT" and str(_dict(evidence.get("run")).get("status") or "") == "SUCCESS":
                # The workflow deadline and process-finalization deadline are
                # distinct. Recompute deliverability from durable evidence;
                # never trust a success flag or infer missing Gate proof.
                result = measure(
                    case,
                    _dict(evidence.get("run")) or {"id": spec["run_id"]},
                    evidence.get("events", []),
                    process_result["duration"],
                )
                if result.get("deliverable"):
                    result["supervisor_outcome"] = "TIMEOUT_AFTER_TERMINAL_DELIVERY"
                    result["finalization_grace_exhausted"] = True
                else:
                    result = measure(case, _dict(evidence.get("run")) or {"id": spec["run_id"]},
                                     evidence.get("events", []), process_result["duration"],
                                     outcome="TIMEOUT", error="Final delivery evidence was incomplete when the supervisor grace expired")
            else:
                result = measure(case, _dict(evidence.get("run")) or {"id": spec["run_id"]},
                                 evidence.get("events", []), process_result["duration"],
                                 outcome=process_result["outcome"] or "FAILED",
                                 error="Run cancelled" if process_result["outcome"] == "CANCELLED" else
                                 "Run total timeout" if process_result["outcome"] == "TIMEOUT" else
                                 "Worker failed; inspect result.json, worker.log and supervisor-error.json")
            result.update({"repetition": repetition, "directory": str(directory), "workflow_sha256": digest,
                           "cohort": case.cohort,
                           "provider_fingerprint": provider_configuration["fingerprint"],
                           "configuration_fingerprint": runtime_configuration["fingerprint"],
                           "workflow_duration_seconds": result.get("duration_seconds"),
                           "supervisor_duration_seconds": round(process_result["duration"], 3),
                           "duration_seconds": round(process_result["duration"], 3)})
            result["first_pass"] = _first_pass(result)
            write_json(directory / "result.json", result)
            results.append(result)
            report = {"mode": "live", "workflow_sha256": digest, "workflow_snapshot": snapshot,
                      "provider": provider_configuration, "configuration": report_configuration,
                      "repeat": args.repeat,
                      "execution_timeout_seconds": args.timeout,
                      "finalize_grace_seconds": args.finalize_grace,
                      "metric_definitions": {"first_pass": "deliverable with zero repairs, retries, continuations, fallback and Run recovery across all attempts; null if attempt evidence is unavailable",
                                             "first_pass_rate": "confirmed first-pass deliverables divided by all Runs (including failures and unknowns)",
                                             "repair_count": "file/part generation repair attempts plus validation repair rounds, deduplicated per node execution",
                                             "retry_count": "node retries, excluding separately counted artifact repairs and continuations",
                                             "recovery_count": "Run recovery count, reported separately from node retries",
                                             "coding_loop": "persisted Plan/Act turns, bounded tool actions, and isolated repair candidates"},
                      "auto_approve": args.auto_approve, "results": results, "summary": aggregate(results)}
            write_json(suite / "report.json", report)
            (suite / "report.md").write_text(render_markdown_report(report), encoding="utf-8")
            print(f"{case.id} #{repetition}: {result['status']} deliverable={result['deliverable']}", flush=True)
            summary = report["summary"]
            print(f"Cumulative success={summary['successes']}/{summary['runs']} ({summary['success_rate']:.1%}); "
                  f"deliverable={summary['deliverables']}/{summary['runs']} ({summary['deliverable_rate']:.1%}); "
                  f"first_pass={summary['first_passes']}/{summary['runs']} ({summary['first_pass_rate']:.1%})", flush=True)
            if result["status"] == "CANCELLED":
                cancelled = True
                break
        if cancelled:
            break
    print(f"Report: {suite / 'report.json'}")
    print(f"Markdown: {suite / 'report.md'}")
    return 130 if cancelled else 0 if all(row["deliverable"] for row in results) else 1


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
