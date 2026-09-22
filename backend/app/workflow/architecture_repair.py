"""Bounded, checkpointed Architecture contract-validation back edge.

This subgraph runs before approval. It never freezes or publishes a candidate:
the executor commits only the validated result returned by this coordinator.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Awaitable, Callable
from copy import deepcopy
from typing import Any, TypedDict

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph

from .platform_contracts import FailureFact


class ArchitectureContractError(ValueError):
    """A known pre-freeze contract defect, not a Provider-format failure."""


class ArchitectureRepairState(TypedDict, total=False):
    run_id: str
    candidate: dict[str, Any]
    result: dict[str, Any]
    failure: dict[str, Any]
    attempt: int
    max_attempts: int
    previous_fingerprint: str
    no_progress: int
    history: list[dict[str, Any]]
    passed: bool
    stopped: bool
    stop_reason: str


Validate = Callable[[dict[str, Any]], dict[str, Any]]
Repair = Callable[[dict[str, Any], dict[str, Any], int], Awaitable[dict[str, Any]]]
Emit = Callable[[str, dict[str, Any]], Awaitable[None]]


def contract_failure_fact(error: ValueError, *, attempt: int) -> dict[str, Any]:
    message = str(error).strip()[:1000]
    lowered = message.lower()
    if "entity_id" in lowered:
        code, scope, summary = "ARCH_API_ENTITY_ID_REQUIRED", ["api_contract"], "API 合同缺少明确的实体关联 entity_id。"
    elif "fields" in lowered or "字段" in message or "实体合同" in message:
        code, scope, summary = "ARCH_ENTITY_FIELDS_INVALID", ["entities"], "实体字段合同格式或类型不合法。"
    elif any(marker in lowered for marker in ("api", "path", "query_parameters", "http")):
        code, scope, summary = "ARCH_API_CONTRACT_INVALID", ["api_contract"], "API 合同格式或路径不合法。"
    elif "architecture decision" in lowered:
        code, scope, summary = "ARCH_DECISION_INVALID", ["entities", "api_contract"], "架构输出不是有效的结构化决策。"
    else:
        code, scope, summary = "ARCH_CONTRACT_UNKNOWN", [], "架构合同校验失败，责任尚未确定。"
    normalized = re.sub(r"\s+", " ", message)
    fingerprint = hashlib.sha256(f"{code}:{normalized}".encode("utf-8")).hexdigest()[:16]
    return FailureFact(
        failure_id=f"architecture_{fingerprint}", code=code,
        category="architecture_contract" if scope else "unclassified",
        owner="architecture" if scope else "platform",
        gate="contract_preflight", stage="architecture_contract",
        expected="合同编译通过后才能进入审批与冻结", actual=message,
        evidence={"exception": type(error).__name__, "message": message},
        related_contract=scope[0] if len(scope) == 1 else "entities/api_contract" if scope else None,
        severity="high", message=message, summary=summary,
        repairable=bool(scope), retryable=bool(scope),
        repair_action="repair_architecture_before_freeze" if scope else "inspect_platform",
        repair_scope=scope, fingerprint=fingerprint, attempt=attempt,
    ).model_dump(mode="json")


def merge_scoped_architecture_patch(
    candidate: dict[str, Any], patch: dict[str, Any], scope: list[str], *, error: str = "",
) -> dict[str, Any]:
    """Accept only rows implicated by the failed rule; retain passing rows."""
    incoming = patch.get("delivery_contract") if isinstance(patch.get("delivery_contract"), dict) else patch
    updated = deepcopy(candidate)
    proposal = dict(updated.get("delivery_contract") or {})
    for section in scope:
        if section not in {"entities", "api_contract"} or not isinstance(incoming.get(section), list):
            continue
        originals = [row for row in proposal.get(section, []) if isinstance(row, dict)]
        suggested = [row for row in incoming[section] if isinstance(row, dict)]

        def key(row: dict[str, Any]) -> str:
            return str(row.get("name") or row.get("id") or "") if section == "entities" else str(row.get("collection_path") or row.get("path") or "")

        def implicated(row: dict[str, Any]) -> bool:
            identifier = key(row)
            mentioned = bool(identifier and identifier in error)
            if section == "entities":
                return mentioned or not isinstance(row.get("fields"), dict) or not row.get("fields")
            named_paths = re.findall(r"/api/[A-Za-z][\w-]*", error)
            return (mentioned or (not named_paths and "entity_id" in error and not row.get("entity_id"))
                    or any(not isinstance(row.get(field), list) for field in ("methods", "fields", "query_parameters") if field in row))

        fixed_keys = {key(row) for row in originals if not implicated(row)}
        available = [row for row in suggested if key(row) not in fixed_keys]
        merged: list[dict[str, Any]] = []
        for index, row in enumerate(originals):
            if not implicated(row):
                merged.append(row)
                continue
            replacement = next((item for item in available if key(item) == key(row)), None)
            if replacement is None and index < len(suggested) and suggested[index] in available:
                replacement = suggested[index]
            if replacement is None and available:
                replacement = available[0]
            if replacement is not None:
                available.remove(replacement)
            merged.append(replacement or row)
        known = {key(row) for row in merged}
        for row in available:
            identifier = key(row)
            if identifier and identifier not in known and (
                identifier in error or (section == "api_contract" and str(row.get("entity_id") or "") in error)
            ):
                merged.append(row)
                known.add(identifier)
        proposal[section] = merged
    updated["delivery_contract"] = proposal
    return updated


def architecture_patch_issue(candidate: dict[str, Any], patch: dict[str, Any], fact: dict[str, Any]) -> str | None:
    """Reject an entity-link repair that still omits the required explicit link.

    This checks the model's proposed patch; it never infers an entity from a URL.
    The complete contract gate remains authoritative after the scoped merge.
    """
    if fact.get("code") != "ARCH_API_ENTITY_ID_REQUIRED":
        return None
    incoming = patch.get("delivery_contract") if isinstance(patch.get("delivery_contract"), dict) else patch
    rows = incoming.get("api_contract")
    if not isinstance(rows, list) or not rows:
        return "修复结果缺少 api_contract 数组，无法确认 API 与实体的关联。"
    entities = (candidate.get("delivery_contract") or {}).get("entities") or []
    allowed = {str(row.get("name") or row.get("id") or "").strip().lower()
               for row in entities if isinstance(row, dict)}
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or not str(row.get("entity_id") or "").strip():
            return f"api_contract[{index}] 仍缺少 entity_id；每条返回的 API 合同都必须明确关联实体。"
        entity_id = str(row["entity_id"]).strip()
        if allowed and entity_id.lower() not in allowed:
            return f"api_contract[{index}] 的 entity_id={entity_id} 不在已有实体清单中。"
    missing_entity = re.search(r"缺少实体\s+([^\s]+)\s+的", str(fact.get("message") or ""))
    if missing_entity and not any(str(row["entity_id"]).lower() == missing_entity.group(1).lower() for row in rows):
        return f"修复结果仍未包含实体 {missing_entity.group(1)} 的 API 合同。"
    return None


class ArchitectureRepairCoordinator:
    def __init__(self, checkpointer: BaseCheckpointSaver | None = None) -> None:
        self.checkpointer = checkpointer

    async def coordinate(
        self,
        *, run_id: str, candidate: dict[str, Any], max_attempts: int,
        validate: Validate, repair: Repair, emit: Emit | None = None,
    ) -> ArchitectureRepairState:
        async def publish(event: str, payload: dict[str, Any]) -> None:
            if emit:
                await emit(event, payload)

        async def validate_node(state: ArchitectureRepairState) -> dict[str, Any]:
            attempt = int(state.get("attempt", 0))
            try:
                result = validate(state["candidate"])
            except ValueError as exc:
                fact = contract_failure_fact(exc, attempt=attempt)
                repeated = bool(state.get("previous_fingerprint") == fact["fingerprint"])
                no_progress = int(state.get("no_progress", 0)) + 1 if repeated else 0
                stopped = not fact["repairable"] or attempt >= int(state["max_attempts"]) or no_progress >= 2
                reason = (
                    "架构合同错误归属不明确，已停止自动改写。" if not fact["repairable"] else
                    "架构合同修复无进展，已熔断。" if no_progress >= 2 else
                    "架构合同修复达到轮次上限。" if stopped else ""
                )
                history = [*state.get("history", []), {"attempt": attempt, "fingerprint": fact["fingerprint"],
                                                     "code": fact["code"], "targetGate": "FAILED"}]
                await publish("architecture.contract_failed", {"repairAttempt": attempt, "failureFact": fact, "stopped": stopped})
                if stopped:
                    await publish("architecture.repair_circuit_open", {"repairAttempt": attempt, "failureFact": fact, "reason": reason})
                return {"result": {}, "failure": fact, "passed": False, "stopped": stopped,
                        "stop_reason": reason, "previous_fingerprint": fact["fingerprint"],
                        "no_progress": no_progress, "history": history}
            await publish("architecture.target_gate_completed", {"repairAttempt": attempt, "passed": True})
            return {"result": result, "failure": {}, "passed": True, "stopped": False,
                    "history": [*state.get("history", []), {"attempt": attempt, "targetGate": "PASSED"}]}

        async def repair_node(state: ArchitectureRepairState) -> dict[str, Any]:
            attempt = int(state.get("attempt", 0)) + 1
            fact = state["failure"]
            await publish("architecture.repair_started", {"repairAttempt": attempt, "failureFact": fact, "owner": "architecture"})
            patch = await repair(state["candidate"], fact, attempt)
            if not isinstance(patch, dict):
                patch = {}
            return {"candidate": merge_scoped_architecture_patch(state["candidate"], patch, fact["repair_scope"], error=fact["message"]),
                    "attempt": attempt}

        builder = StateGraph(ArchitectureRepairState)
        builder.add_node("contract_gate", validate_node)
        builder.add_node("architecture_repair", repair_node)
        builder.add_edge(START, "contract_gate")
        builder.add_conditional_edges("contract_gate", lambda state: "done" if state.get("passed") or state.get("stopped") else "repair",
                                      {"done": END, "repair": "architecture_repair"})
        builder.add_edge("architecture_repair", "contract_gate")
        graph = builder.compile(checkpointer=self.checkpointer)
        config = {"configurable": {"thread_id": f"{run_id}:architecture-contract"}}
        initial: ArchitectureRepairState = {"run_id": run_id, "candidate": candidate,
                                             "attempt": 0, "max_attempts": max(0, min(3, int(max_attempts))),
                                             "previous_fingerprint": "", "no_progress": 0,
                                             "history": [], "passed": False, "stopped": False}
        if self.checkpointer:
            snapshot = await graph.aget_state(config)
            values = snapshot.values if isinstance(snapshot.values, dict) else {}
            if values.get("run_id") == run_id:
                if snapshot.next:
                    return await graph.ainvoke(None, config=config)
                if values.get("passed") or values.get("stopped"):
                    return values
        return await graph.ainvoke(initial, config=config)
