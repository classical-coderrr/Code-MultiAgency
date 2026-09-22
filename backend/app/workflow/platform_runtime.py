"""Platform Runtime boundary for model policy and request budgets.

This is intentionally a thin facade over the already-tested budget/provider
components. Moving callers behind this boundary lets the platform add queues,
rate limits and model escalation without changing Agent YAML or the DAG.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .budget import TokenBudgetManager


@dataclass(slots=True)
class RuntimePolicy:
    max_tokens: int
    retry_count: int
    thinking: str
    timeout_seconds: float
    model_tier: str = "default"
    escalation_enabled: bool = True


class PlatformRuntime:
    def __init__(self, budget_manager: TokenBudgetManager | None = None) -> None:
        self.budget_manager = budget_manager or TokenBudgetManager()

    def policy_for(self, runtime: dict[str, Any], provider_capabilities: dict[str, Any]) -> RuntimePolicy:
        requested = int(runtime.get("max_tokens") or 1)
        max_tokens = min(requested, self.budget_manager.provider_max_tokens(provider_capabilities))
        return RuntimePolicy(
            max_tokens=max_tokens,
            retry_count=max(0, int(runtime.get("retry") or 0)),
            thinking=str(runtime.get("thinking") or "auto"),
            timeout_seconds=float(runtime.get("timeout_seconds") or runtime.get("timeout") or 120),
            model_tier=str(runtime.get("model_tier") or "default"),
            escalation_enabled=bool(runtime.get("model_escalation", True)),
        )

    @staticmethod
    def failure_route(failure: dict[str, Any]) -> dict[str, Any]:
        """Return a deterministic owner/gate route for Repair Engine."""
        return {
            "owner": str(failure.get("owner") or "reviewer"),
            "gate": str(failure.get("gate") or "integration"),
            "file": failure.get("file"),
            "severity": str(failure.get("severity") or "medium"),
        }
