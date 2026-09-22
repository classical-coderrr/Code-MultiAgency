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
import subprocess
import sys
import time
import uuid
from dataclasses import asdict, dataclass
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any, Callable

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


def load_cases(path: Path | None = None) -> tuple[Case, ...]:
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
        case = Case(item.get("id", ""), item.get("requirement", ""), item.get("expected_stack", ""), answers)
        if not isinstance(case.id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", case.id):
            raise ValueError("invalid case id")
        if not isinstance(case.requirement, str) or not case.requirement.strip():
            raise ValueError("case requirement must be nonempty")
        if not isinstance(case.expected_stack, str) or case.expected_stack not in STACKS:
            raise ValueError("unsupported expected_stack")
        cases.append(case)
    if len({case.id for case in cases}) != len(cases):
        raise ValueError("duplicate case id")
    return tuple(cases)


def _number(value: Any) -> int:
    try:
        return max(0, int(value))
    except (ValueError, TypeError, OverflowError):
        return 0


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
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(redact(value), ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    temporary.replace(path)


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
            "first_pass": _first_pass({"deliverable": deliverable, **corrections}),
            "delivery_contract": contract, "delivery_gate": gate, "gate_source": gate_source}


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
                    "continuation_count", "fallback_count", "recovery_count")}}
    return {**totals(results), "by_case": {case_id: totals([row for row in results if row["case_id"] == case_id])
                                          for case_id in sorted({row["case_id"] for row in results})}}


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
            elif status == "WAITING_CLARIFICATION":
                request = _dict(run.get("clarification"))
                fields = {str(field) for field in request.get("unresolved_fields") or []}
                answers = _dict(clarification_answers)
                if not fields or not fields.issubset(answers):
                    return "NEEDS_INPUT"
                request_id = str(request.get("request_id") or "")
                if request_id and request_id != clarified:
                    await executor.clarify(run_id, {field: answers[field] for field in fields})
                    clarified = request_id
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
              process_factory: Callable[..., Any] = subprocess.Popen,
              tree_factory: Callable[..., Any] = ProcessTree) -> dict[str, Any]:
    start = time.monotonic()
    outcome = None
    process = None
    tree = None
    with (directory / "worker.log").open("wb") as log:
        try:
            process = process_factory(command, cwd=directory, stdout=log, stderr=subprocess.STDOUT,
                                      start_new_session=os.name != "nt")
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


def real_provider() -> Any:
    from dotenv import load_dotenv
    from app.llm.factory import ProviderFactory
    from app.llm.mock import MockProvider

    load_dotenv(BACKEND / ".env", override=False)
    provider = ProviderFactory.create_from_env()
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
    # Local Provider must also stay within this Run's controlled workspace.
    if hasattr(provider, "working_directory"):
        provider.working_directory = str(directory / "workspace")
    workflow = parse_workflow_file(directory / "workflow.yaml", spec["workflow_id"])
    build_dag(workflow)
    repository = SQLiteRepository(directory / "run.sqlite3")
    artifacts = ArtifactService(repository, directory / "workspace")
    bus = WorkflowEventBus(repository)
    executor = WorkflowExecutor(AgentRegistry.from_directory(BACKEND / "agents"), provider, bus, repository,
                                artifact_service=artifacts,
                                skill_registry=SkillRegistry.from_directories([BACKEND / "skills"]))
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
    parser.add_argument("--case-id", action="append", default=[], help="run only matching case ID; repeat to select several")
    parser.add_argument("--repeat", type=_bounded_int, default=1)
    parser.add_argument("--timeout", type=_positive, default=900)
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

    for key, secret in dotenv_values(BACKEND / ".env").items():
        if secret and len(secret) >= 6 and re.search(r"key|secret|password|token|cookie|authorization", key, re.I):
            _SECRET_VALUES.add(secret)

    # Read once. Every repeat uses the same current, validated snapshot.
    yaml_text = args.workflow.resolve().read_text(encoding="utf-8")
    workflow = parse_workflow_yaml(yaml_text, args.workflow.stem)
    build_dag(workflow)
    snapshot = json.loads(json.dumps(asdict(workflow)))
    digest = hashlib.sha256(yaml_text.encode("utf-8")).hexdigest()
    suite = args.output.resolve() / (time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8])
    suite.mkdir(parents=True, exist_ok=False)
    write_json(suite / "workflow-snapshot.json", snapshot)
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
                    "auto_approve": args.auto_approve, "max_retries": args.max_retries}
            write_json(directory / "spec.json", spec)
            process_result = supervise([rtk, "proxy", sys.executable, str(Path(__file__).resolve()),
                                        "--worker", str(directory)], directory, timeout=args.timeout,
                                       grace=args.cancel_grace,
                                       cancel_requested=lambda: (suite / "cancel").exists())
            evidence = salvage(directory, spec)
            if (directory / "result.json").exists() and not process_result["outcome"]:
                result = json.loads((directory / "result.json").read_text(encoding="utf-8"))
                if process_result["returncode"] != 0:
                    result.update(status="FAILED", success=False, deliverable=False)
            else:
                result = measure(case, _dict(evidence.get("run")) or {"id": spec["run_id"]},
                                 evidence.get("events", []), process_result["duration"],
                                 outcome=process_result["outcome"] or "FAILED",
                                 error="Run cancelled" if process_result["outcome"] == "CANCELLED" else
                                 "Run total timeout" if process_result["outcome"] == "TIMEOUT" else
                                 "Worker failed; inspect result.json, worker.log and supervisor-error.json")
            result.update({"repetition": repetition, "directory": str(directory), "workflow_sha256": digest,
                           "duration_seconds": round(process_result["duration"], 3)})
            result["first_pass"] = _first_pass(result)
            write_json(directory / "result.json", result)
            results.append(result)
            report = {"mode": "live", "workflow_sha256": digest, "workflow_snapshot": snapshot,
                      "metric_definitions": {"first_pass": "deliverable with zero repairs, retries, continuations, fallback and Run recovery across all attempts; null if attempt evidence is unavailable",
                                             "first_pass_rate": "confirmed first-pass deliverables divided by all Runs (including failures and unknowns)",
                                             "repair_count": "file/part generation repair attempts plus validation repair rounds, deduplicated per node execution",
                                             "retry_count": "node retries, excluding separately counted artifact repairs and continuations",
                                             "recovery_count": "Run recovery count, reported separately from node retries"},
                      "auto_approve": args.auto_approve, "results": results, "summary": aggregate(results)}
            write_json(suite / "report.json", report)
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
    return 130 if cancelled else 0 if all(row["deliverable"] for row in results) else 1


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
