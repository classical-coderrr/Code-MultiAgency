"""Provider contract. Executor depends on this interface only."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


THINKING_MODES = ("auto", "off", "low", "high", "max")


class LLMError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = False, response_metadata: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.response_metadata = response_metadata or {}


class LLMTimeoutError(LLMError):
    def __init__(self, message: str = "LLM request timed out") -> None:
        super().__init__(message, retryable=True)


@dataclass(slots=True)
class LLMResponse:
    text: str
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = "mock"
    finish_reason: str | None = None
    message_content: str | None = None
    reasoning_content: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    request_parameters: dict[str, Any] = field(default_factory=dict)

    def provider_record(self) -> dict[str, Any]:
        """Return the response fields needed for diagnostics and retry decisions."""
        return {
            "finish_reason": self.finish_reason,
            "message_content": self.text if self.message_content is None else self.message_content,
            "reasoning_content": self.reasoning_content,
            "usage": self.usage,
            "model": self.model,
            # Only non-secret generation controls are recorded. Prompts,
            # headers and API keys must never be included here.
            "request_parameters": self.request_parameters,
        }


class LLMProvider(ABC):
    def capabilities(self) -> dict[str, Any]:
        """Return normalized capabilities for the configured provider/model."""
        return {
            "provider": "unknown",
            "model": "unknown",
            "supportsThinking": False,
            "supportedLevels": ["off"],
            "defaultLevel": "off",
            "maxTokens": {"min": 1, "max": 6000},
        }

    def resolve_thinking(self, requested: str | None) -> tuple[str, str | None]:
        """Resolve a portable thinking level without sending unsupported values."""
        requested_level = str(requested or "auto").strip().lower()
        if requested_level not in THINKING_MODES:
            requested_level = "auto"
        capabilities = self.capabilities()
        supported = [str(level).lower() for level in capabilities.get("supportedLevels", ["off"])]
        if not capabilities.get("supportsThinking", False):
            effective = "off"
        elif requested_level == "auto":
            default_level = str(capabilities.get("defaultLevel", "off")).lower()
            effective = default_level if default_level in supported else supported[-1]
        elif requested_level in supported:
            effective = requested_level
        else:
            fallback_order = {
                "low": ["off", "high", "max"],
                "high": ["low", "off", "max"],
                "max": ["high", "low", "off"],
                "off": ["low", "high", "max"],
            }[requested_level]
            effective = next((level for level in fallback_order if level in supported), supported[0] if supported else "off")
        warning = None if requested_level in {"auto", effective} else (
            f"Requested thinking level '{requested_level}' is not supported; using '{effective}'."
        )
        if requested_level != "auto" and not capabilities.get("supportsThinking", False):
            warning = "This model does not support thinking controls; thinking was turned off."
        return effective, warning

    @abstractmethod
    async def generate(self, system_prompt: str, user_prompt: str, config: dict[str, Any] | None = None) -> LLMResponse:
        raise NotImplementedError
