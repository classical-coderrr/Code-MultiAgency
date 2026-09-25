"""Persist final delivery-gate evidence and unresolved failure facts."""
from __future__ import annotations

import json
from typing import Any

from .platform_contracts import evidence_from_check, failure_fact_from_check


async def record_delivery_gate_evidence(
    state: Any,
    gate: dict[str, Any],
    repository: Any,
    event_bus: Any,
) -> None:
    checks = gate.get("checks") if isinstance(gate, dict) else []
    if not isinstance(checks, list):
        return
    evidence = [
        evidence_from_check(item, gate="delivery", index=index)
        for index, item in enumerate(checks)
        if isinstance(item, dict)
    ]
    failures = [
        failure_fact_from_check(
            item,
            gate="delivery",
            owner="platform" if str(item.get("id") or "") == "delivery-archive" else "integration_gate",
            index=index,
        )
        for index, item in enumerate(checks)
        if isinstance(item, dict) and str(item.get("status")) != "passed"
    ]
    snapshot = state.context.snapshot()
    prior_evidence = snapshot.get("evidence")
    retained_evidence = [
        item for item in (prior_evidence if isinstance(prior_evidence, list) else [])
        if not isinstance(item, dict) or str(item.get("gate") or "") != "delivery"
    ]
    all_evidence = [*retained_evidence, *evidence]
    state.context.set("delivery_evidence", evidence)
    state.context.set("evidence", all_evidence)

    prior_failures = snapshot.get("failure_facts")
    historical = [item for item in (prior_failures if isinstance(prior_failures, list) else []) if isinstance(item, dict)]
    active_ids = {str(item.get("failure_id") or "") for item in failures}
    retained_failures = [
        item if str(item.get("gate") or "") != "delivery"
        else {**item, "resolved": True}
        for item in historical
        if str(item.get("gate") or "") != "delivery" or str(item.get("failure_id") or "") not in active_ids
    ]
    all_failures = [*retained_failures, *failures]
    state.context.set("failure_facts", all_failures)
    repository.update_run(
        state.run_id,
        evidence_json=json.dumps(all_evidence, ensure_ascii=False),
        failure_facts_json=json.dumps(all_failures, ensure_ascii=False),
    )
    await event_bus.emit(
        "integration.evidence",
        state.run_id,
        {"evidence": evidence, "failureFacts": failures},
    )
