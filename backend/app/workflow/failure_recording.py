"""Durable workflow failure-fact recording."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from ..repositories.sqlite import SQLiteRepository
from .run_state import RunState


class FailureFactRecorder:
    def __init__(self, repository: SQLiteRepository) -> None:
        self.repository = repository

    def record_node_failure(
        self,
        state: RunState,
        step_id: str,
        error: str,
        route: dict[str, Any],
    ) -> dict[str, Any]:
        """Persist a node failure even when a local repair later succeeds."""

        snapshot = state.context.snapshot()
        previous = snapshot.get("failure_facts")
        facts = list(previous) if isinstance(previous, list) else []
        digest = hashlib.sha256(str(error).encode("utf-8")).hexdigest()
        failure_id = f"failure_{state.run_id}_{step_id}_{digest[:10]}"
        facts = [
            item
            for item in facts
            if not isinstance(item, dict) or item.get("failure_id") != failure_id
        ]
        fact = {
            "failure_id": failure_id,
            "code": str(
                route.get("error_type") or route.get("category") or "NODE_FAILURE"
            ).upper(),
            "category": route.get("category", "node_execution"),
            "owner": str(route.get("owner_step") or step_id),
            "gate": "workflow",
            "evidence": {
                "step_id": step_id,
                "exception": route.get("error_type"),
                "message": str(error)[:1000],
            },
            "severity": route.get("severity", "medium"),
            "message": str(error)[:1000],
            "summary": str(error).split("；", 1)[0].split("\n", 1)[0][:240],
            "stage": route.get("stage", "execution"),
            "repairable": bool(route.get("repairable", False)),
            "retryable": bool(route.get("retryable", False)),
            "repair_action": route.get("action", "retry_same_node"),
            "repair_scope": (
                [str(route.get("owner_step") or step_id)]
                if route.get("repairable")
                else []
            ),
            "fingerprint": digest[:16],
            "attempt": 0,
        }
        facts.append(fact)
        state.context.set("failure_facts", facts)
        self.repository.update_run(
            state.run_id,
            failure_facts_json=json.dumps(facts, ensure_ascii=False),
        )
        return fact

