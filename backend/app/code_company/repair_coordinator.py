"""Checkpoint-friendly repair subgraph with bounded conditional back edges."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from ..workflow.artifact_validator import ArtifactValidationResult
from .repair import RepairEngine


RepairCallback = Callable[[dict[str, Any], int, str], Awaitable[tuple[list[str], list[Any]]]]
ValidateTargetCallback = Callable[[dict[str, Any], int], Awaitable[ArtifactValidationResult]]
ValidateFullCallback = Callable[[int], Awaitable[ArtifactValidationResult]]
LifecycleCallback = Callable[[dict[str, Any], int], Awaitable[None]]
EmitCallback = Callable[[str, dict[str, Any]], Awaitable[None]]
ConsultCallback = Callable[[dict[str, Any], ArtifactValidationResult, int], Awaitable[dict[str, Any]]]


class RepairGraphState(TypedDict, total=False):
    validation: ArtifactValidationResult
    pre_repair_validation: ArtifactValidationResult
    protected_passed_checks: list[str]
    plan: dict[str, Any]
    attempt: int
    max_attempts: int
    no_progress_count: int
    previous_fingerprint: str
    repaired_files: list[str]
    responses: list[Any]
    target_passed: bool
    full_passed: bool
    stopped: bool
    stop_reason: str
    history: list[dict[str, Any]]


@dataclass(frozen=True, slots=True)
class RepairOutcome:
    validation: ArtifactValidationResult
    passed: bool
    attempts: int
    no_progress_count: int
    repaired_files: tuple[str, ...]
    responses: tuple[Any, ...]
    plan: dict[str, Any]
    history: tuple[dict[str, Any], ...]
    stop_reason: str = ""


class RepairCoordinator:
    """Execute repair -> target Gate -> full regression as a LangGraph loop.

    The main workflow remains an acyclic delivery graph. Repair is a bounded
    subgraph with explicit conditional back edges, which prevents a dynamic
    defect loop from corrupting the user-authored YAML DAG.
    """

    def __init__(self, repair_engine: RepairEngine | None = None) -> None:
        self.repair_engine = repair_engine or RepairEngine()

    async def coordinate(
        self,
        initial_validation: ArtifactValidationResult,
        *,
        max_attempts: int,
        repair: RepairCallback,
        validate_target: ValidateTargetCallback,
        validate_full: ValidateFullCallback,
        prepare_candidate: LifecycleCallback | None = None,
        reject_candidate: LifecycleCallback | None = None,
        commit_candidate: LifecycleCallback | None = None,
        emit: EmitCallback | None = None,
        consult: ConsultCallback | None = None,
    ) -> RepairOutcome:
        async def publish(event_type: str, payload: dict[str, Any]) -> None:
            if emit:
                await emit(event_type, payload)

        async def plan_node(state: RepairGraphState) -> dict[str, Any]:
            attempt = int(state.get("attempt", 0)) + 1
            plan = self.repair_engine.plan_validation_repair(
                state["validation"],
                attempt=attempt,
                previous_fingerprint=str(state.get("previous_fingerprint") or ""),
                no_progress_count=int(state.get("no_progress_count", 0)),
            )
            stopped = attempt > int(state.get("max_attempts", 0)) or not bool(plan.get("repairable"))
            reason = "已达到最大自动修复轮数。" if attempt > int(state.get("max_attempts", 0)) else str(plan.get("reason") or "")
            if not stopped and attempt == 1 and len(plan.get("owners") or []) > 1 and consult:
                try:
                    consultation = await consult(plan, state["validation"], attempt)
                    if isinstance(consultation, dict):
                        plan = {**plan, "consultation": consultation}
                except Exception as exc:
                    # Consultation is advisory; it must never bypass or block
                    # the deterministic owner repair and verification path.
                    await publish("collaboration.unavailable", {
                        "repairAttempt": attempt, "reason": f"{type(exc).__name__}: {str(exc)[:160]}",
                    })
            await publish("repair.routed", {"repairAttempt": attempt, "plan": plan, "reason": reason})
            if stopped:
                await publish(
                    "repair.circuit_open" if plan.get("circuit_open") else "repair.escalated",
                    {"repairAttempt": attempt, "plan": plan, "reason": reason},
                )
            return {"plan": plan, "stopped": stopped, "stop_reason": reason}

        async def repair_node(state: RepairGraphState) -> dict[str, Any]:
            attempt = int(state.get("attempt", 0)) + 1
            plan = dict(state.get("plan") or {})
            if prepare_candidate:
                await prepare_candidate(plan, attempt)
            await publish("repair.round_started", {"repairAttempt": attempt, "plan": plan})
            files, responses = await repair(plan, attempt, str(plan.get("strategy") or "targeted_file"))
            history = [*state.get("history", []), {
                "attempt": attempt,
                "owners": list(plan.get("owners") or []),
                "checkIds": list(plan.get("check_ids") or []),
                "strategy": plan.get("strategy"),
                "files": list(files),
                "targetGate": "PENDING",
                "fullRegression": "PENDING",
            }]
            return {
                "attempt": attempt,
                "pre_repair_validation": state["validation"],
                "repaired_files": [*state.get("repaired_files", []), *files],
                "responses": [*state.get("responses", []), *responses],
                "history": history,
                "target_passed": False,
                "full_passed": False,
            }

        async def target_gate_node(state: RepairGraphState) -> dict[str, Any]:
            attempt = int(state.get("attempt", 0))
            plan = dict(state.get("plan") or {})
            files = list(state.get("history", [])[-1].get("files", [])) if state.get("history") else []
            before = state["validation"]
            if not files:
                result = before
                progress = False
            else:
                result = await validate_target(plan, attempt)
                progress = self.repair_engine.made_progress(before, result)
            history = list(state.get("history", []))
            previous_failure_keys = sorted(self.repair_engine.compiler_failure_keys(before))
            current_failure_keys = sorted(self.repair_engine.compiler_failure_keys(result))
            resolved_failure_keys = sorted(set(previous_failure_keys) - set(current_failure_keys))
            if history:
                history[-1] = {
                    **history[-1],
                    "targetGate": "PASSED" if result.passed else "FAILED",
                    "madeProgress": progress,
                    "resolvedFailureKeys": resolved_failure_keys,
                    "currentFailureKeys": current_failure_keys,
                }
            no_progress = 0 if progress else int(state.get("no_progress_count", 0)) + 1
            if not progress and reject_candidate:
                await reject_candidate(plan, attempt)
            await publish("repair.target_gate_completed", {
                "repairAttempt": attempt,
                "owners": plan.get("owners", []),
                "passed": result.passed,
                "madeProgress": progress,
                "resolvedFailureKeys": resolved_failure_keys,
                "currentFailureKeys": current_failure_keys,
                "stageTransition": (
                    "已通过：" + ("、".join(check.label for check in result.checks if check.status == "passed")[:120]
                                 or "上轮错误已消除")
                    + " → 当前失败：" + "、".join(check.label for check in result.checks if check.status == "failed")[:120]
                ) if progress and not result.passed else "",
                "result": result.as_dict(),
            })
            return {
                "validation": result if progress else before,
                "target_passed": result.passed,
                "no_progress_count": no_progress,
                "previous_fingerprint": self.repair_engine.fingerprint(before),
                "history": history,
                "stopped": no_progress >= 2,
                "stop_reason": "连续两轮修复未推进目标 Gate。" if no_progress >= 2 else "",
            }

        async def full_gate_node(state: RepairGraphState) -> dict[str, Any]:
            attempt = int(state.get("attempt", 0))
            plan = dict(state.get("plan") or {})
            before = state.get("pre_repair_validation", initial_validation)
            result = await validate_full(attempt)
            protected = set(state.get("protected_passed_checks", []))
            full_statuses = {check.id: check.status for check in result.checks}
            regressed = sorted(check_id for check_id in protected if full_statuses.get(check_id) == "failed")
            unverified = sorted(check_id for check_id in protected if full_statuses.get(check_id) not in {"passed", "failed"})
            # A fail-fast validation may not reach later checks. Absence is not
            # proof of regression, but a successful full run must reprove them.
            progress = (self.repair_engine.made_progress(before, result) and not regressed
                        and not (result.passed and unverified))
            full_passed = result.passed and progress
            history = list(state.get("history", []))
            if history:
                history[-1] = {**history[-1], "fullRegression": "PASSED" if full_passed else "FAILED", "madeProgress": progress,
                               "regressedChecks": regressed, "unverifiedChecks": unverified}
            no_progress = 0 if progress else int(state.get("no_progress_count", 0)) + 1
            if full_passed and commit_candidate:
                await commit_candidate(plan, attempt)
            elif not progress and reject_candidate:
                await reject_candidate(plan, attempt)
            await publish("repair.full_regression_completed", {
                "repairAttempt": attempt,
                "passed": full_passed,
                "madeProgress": progress,
                "regressedChecks": regressed,
                "unverifiedChecks": unverified,
                "result": result.as_dict(),
            })
            return {
                "validation": result if progress else before,
                "full_passed": full_passed,
                "protected_passed_checks": sorted(protected | {
                    check.id for check in result.checks if progress and check.status == "passed"
                }),
                "no_progress_count": no_progress,
                "previous_fingerprint": self.repair_engine.fingerprint(before),
                "history": history,
                "stopped": no_progress >= 2,
                "stop_reason": "完整回归连续无进展或破坏已通过检查，已触发熔断。" if no_progress >= 2 else "",
            }

        builder = StateGraph(RepairGraphState)
        builder.add_node("plan", plan_node)
        builder.add_node("repair", repair_node)
        builder.add_node("target_gate", target_gate_node)
        builder.add_node("full_regression", full_gate_node)
        builder.add_edge(START, "plan")
        builder.add_conditional_edges("plan", self._route_plan, {"repair": "repair", "stop": END})
        builder.add_edge("repair", "target_gate")
        builder.add_conditional_edges(
            "target_gate",
            self._route_target_gate,
            {"full": "full_regression", "retry": "plan", "stop": END},
        )
        builder.add_conditional_edges(
            "full_regression",
            self._route_full_gate,
            {"done": END, "retry": "plan", "stop": END},
        )
        graph = builder.compile()
        final = await graph.ainvoke({
            "validation": initial_validation,
            "protected_passed_checks": [check.id for check in initial_validation.checks if check.status == "passed"],
            "attempt": 0,
            "max_attempts": max(0, int(max_attempts)),
            "no_progress_count": 0,
            "previous_fingerprint": "",
            "repaired_files": [],
            "responses": [],
            "history": [],
            "stopped": False,
            "stop_reason": "",
        })
        validation = final.get("validation") or initial_validation
        outcome = RepairOutcome(
            validation=validation,
            passed=bool(validation.passed and final.get("full_passed")),
            attempts=int(final.get("attempt", 0)),
            no_progress_count=int(final.get("no_progress_count", 0)),
            repaired_files=tuple(dict.fromkeys(str(item) for item in final.get("repaired_files", []))),
            responses=tuple(final.get("responses", [])),
            plan=dict(final.get("plan") or {}),
            history=tuple(final.get("history", [])),
            stop_reason=str(final.get("stop_reason") or ""),
        )
        await publish("repair.completed", {
            "passed": outcome.passed,
            "attempts": outcome.attempts,
            "noProgressCount": outcome.no_progress_count,
            "files": list(outcome.repaired_files),
            "history": list(outcome.history),
            "reason": outcome.stop_reason,
        })
        return outcome

    @staticmethod
    def _route_plan(state: RepairGraphState) -> str:
        return "stop" if state.get("stopped") else "repair"

    @staticmethod
    def _route_target_gate(state: RepairGraphState) -> str:
        if state.get("target_passed"):
            return "full"
        if state.get("stopped") or int(state.get("attempt", 0)) >= int(state.get("max_attempts", 0)):
            return "stop"
        return "retry"

    @staticmethod
    def _route_full_gate(state: RepairGraphState) -> str:
        if state.get("full_passed"):
            return "done"
        if state.get("stopped") or int(state.get("attempt", 0)) >= int(state.get("max_attempts", 0)):
            return "stop"
        return "retry"
