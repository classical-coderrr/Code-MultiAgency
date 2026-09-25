"""Portable model capability profiles for OpenAI-compatible endpoints.

The application uses one internal thinking contract. This module is the
boundary that maps a model family to the provider-specific wire format. A
provider name alone is never treated as proof of model capabilities.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse


PORTABLE_LEVELS = ("off", "low", "high", "max")


@dataclass(frozen=True, slots=True)
class ModelProfile:
    family: str
    protocol: str
    supports_thinking: bool
    default_level: str = "off"
    max_output_tokens: int = 6000
    transport: str = "openai_compatible"
    configuration_warning: str | None = None
    supports_json_mode: bool = False

    @property
    def supported_levels(self) -> tuple[str, ...]:
        return PORTABLE_LEVELS if self.supports_thinking else ("off",)

    def capabilities(self, provider_name: str, model_name: str) -> dict[str, object]:
        result: dict[str, object] = {
            "provider": provider_name,
            "transport": self.transport,
            "modelFamily": self.family,
            "model": model_name,
            "supportsThinking": self.supports_thinking,
            "supportedLevels": list(self.supported_levels),
            "defaultLevel": self.default_level if self.default_level in self.supported_levels else "off",
            "maxTokens": {"min": 1, "max": self.max_output_tokens},
            "capabilitySource": "model_family_registry",
        }
        if self.configuration_warning:
            result["configurationWarning"] = self.configuration_warning
        return result


def resolve_model_profile(provider_name: str, base_url: str, model_name: str) -> ModelProfile:
    """Resolve capabilities from model family first, endpoint second.

    Unknown models deliberately use a conservative no-thinking profile. The
    user can still call any OpenAI-compatible endpoint, but the runtime will
    not invent vendor-specific request fields for an unverified model.
    """
    provider = provider_name.strip().lower()
    model = model_name.strip().lower()
    host = (urlparse(base_url).hostname or "").lower()

    if _is_glm_model(model) or "bigmodel.cn" in host:
        warning = None
        if "bigmodel.cn" not in host and (provider == "qwen" or "dashscope.aliyuncs.com" in host):
            warning = "GLM 模型与当前 Base URL 不匹配；请使用智谱兼容接口或确认网关确实提供该模型。"
        return ModelProfile(
            family="glm",
            protocol="glm",
            supports_thinking=True,
            default_level="max",
            max_output_tokens=65536,
            configuration_warning=warning,
        )

    if model.startswith("deepseek-v4") and _is_dashscope_endpoint(provider, host):
        return ModelProfile(
            family="deepseek_v4",
            protocol="dashscope_deepseek",
            supports_thinking=True,
            default_level="high",
            transport="dashscope_openai_compatible",
        )

    if model.startswith("deepseek"):
        return ModelProfile(
            family="deepseek",
            protocol="deepseek",
            supports_thinking=True,
            default_level="high",
            supports_json_mode=True,
        )

    if model.startswith("qwen3"):
        return ModelProfile(
            family="qwen3",
            protocol="qwen",
            supports_thinking=True,
            default_level="max" if model.startswith("qwen3.8") else "high",
            supports_json_mode=True,
        )

    return ModelProfile(family="unknown", protocol="generic", supports_thinking=False)


def _is_glm_model(model: str) -> bool:
    return model.startswith("glm-") or model.startswith("chatglm-")


def _is_dashscope_endpoint(provider: str, host: str) -> bool:
    return provider == "qwen" or "dashscope.aliyuncs.com" in host or ".maas.aliyuncs.com" in host
