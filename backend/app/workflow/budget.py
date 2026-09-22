"""Provider-aware token budgeting for every workflow step."""

from __future__ import annotations

import json
from dataclasses import dataclass
from math import ceil
from typing import Any


@dataclass(frozen=True, slots=True)
class BudgetDecision:
    requested_max_tokens: int
    effective_max_tokens: int
    input_tokens: int
    context_window: int
    provider_max_tokens: int
    reserved_tokens: int
    warnings: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "requestedMaxTokens": self.requested_max_tokens,
            "effectiveMaxTokens": self.effective_max_tokens,
            "inputTokens": self.input_tokens,
            "contextWindow": self.context_window,
            "providerMaxTokens": self.provider_max_tokens,
            "reservedTokens": self.reserved_tokens,
            "warnings": list(self.warnings),
        }


class TokenBudgetManager:
    """Calculate safe per-request budgets without assuming a specific model."""

    def __init__(self, *, default_context_window: int = 32768, safety_margin: int = 512) -> None:
        self.default_context_window = max(1024, default_context_window)
        self.safety_margin = max(64, safety_margin)

    @staticmethod
    def estimate_tokens(value: Any) -> int:
        if isinstance(value, (dict, list)):
            text = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
        else:
            text = str(value or "")
        return max(1, ceil(len(text) / 4)) if text else 0

    def context_window(self, capabilities: dict[str, Any] | None) -> int:
        raw = (capabilities or {}).get("contextWindow", self.default_context_window)
        try:
            return max(1024, int(raw))
        except (TypeError, ValueError):
            return self.default_context_window

    @staticmethod
    def provider_max_tokens(capabilities: dict[str, Any] | None) -> int:
        raw = (capabilities or {}).get("maxTokens", {})
        if isinstance(raw, dict):
            raw = raw.get("max", 6000)
        try:
            return max(1, int(raw))
        except (TypeError, ValueError):
            return 6000

    def input_budget_tokens(
        self,
        requested_max_tokens: int,
        capabilities: dict[str, Any] | None = None,
        *,
        reserved_tokens: int | None = None,
    ) -> int:
        context_window = self.context_window(capabilities)
        provider_max = self.provider_max_tokens(capabilities)
        requested = max(1, int(requested_max_tokens))
        reserve = self.safety_margin if reserved_tokens is None else max(64, int(reserved_tokens))
        return max(256, context_window - min(requested, provider_max) - reserve)

    def plan(
        self,
        system_prompt: str,
        user_prompt: str,
        requested_max_tokens: int,
        capabilities: dict[str, Any] | None = None,
        *,
        reserved_tokens: int | None = None,
    ) -> BudgetDecision:
        context_window = self.context_window(capabilities)
        provider_max = self.provider_max_tokens(capabilities)
        requested = max(1, int(requested_max_tokens))
        reserve = self.safety_margin if reserved_tokens is None else max(64, int(reserved_tokens))
        input_tokens = self.estimate_tokens(f"{system_prompt}\n{user_prompt}")
        available = max(1, context_window - input_tokens - reserve)
        effective = min(requested, provider_max, available)
        warnings: list[str] = []
        if effective < requested:
            warnings.append(
                f"Output budget reduced from {requested} to {effective} to fit provider context limits."
            )
        if input_tokens + reserve >= context_window:
            warnings.append("Prompt is close to the provider context limit; context packing is required.")
        return BudgetDecision(
            requested_max_tokens=requested,
            effective_max_tokens=max(1, effective),
            input_tokens=input_tokens,
            context_window=context_window,
            provider_max_tokens=provider_max,
            reserved_tokens=reserve,
            warnings=tuple(warnings),
        )
