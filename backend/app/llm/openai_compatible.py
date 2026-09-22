"""OpenAI-compatible provider with cancellable async HTTP requests."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from .base import LLMError, LLMProvider, LLMResponse, LLMTimeoutError
from .model_profiles import resolve_model_profile


class OpenAICompatibleProvider(LLMProvider):
    def __init__(self, base_url: str, api_key: str, model_name: str, provider_name: str = "openai_compatible") -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model_name = model_name
        self.provider_name = provider_name.strip().lower()

    def capabilities(self) -> dict[str, Any]:
        return resolve_model_profile(self.provider_name, self.base_url, self.model_name).capabilities(self.provider_name, self.model_name)

    async def generate(self, system_prompt: str, user_prompt: str, config: dict[str, Any] | None = None) -> LLMResponse:
        settings = config or {}
        payload = {
            "model": self.model_name,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
        max_tokens = _positive_int(settings.get("max_tokens"), 6000)
        payload["max_tokens"] = max_tokens
        profile = resolve_model_profile(self.provider_name, self.base_url, self.model_name)
        if profile.protocol == "deepseek":
            effective_thinking = str(settings.get("effective_thinking", "auto")).strip().lower()
            thinking_type = str(settings.get("thinking_type", "enabled")).strip().lower()
            if effective_thinking == "off":
                thinking_type = "disabled"
            payload["thinking"] = {"type": "disabled" if thinking_type == "disabled" else "enabled"}
            if payload["thinking"]["type"] == "enabled":
                effort = str(settings.get("reasoning_effort", effective_thinking if effective_thinking != "auto" else "high")).strip().lower()
                payload["reasoning_effort"] = effort if effort in {"low", "high", "max"} else "high"
        elif profile.protocol == "dashscope_deepseek":
            effective_thinking = str(settings.get("effective_thinking", "auto")).strip().lower()
            if effective_thinking == "off":
                payload["enable_thinking"] = False
            else:
                payload["enable_thinking"] = True
                effort = str(settings.get("reasoning_effort", effective_thinking if effective_thinking != "auto" else "high")).strip().lower()
                payload["reasoning_effort"] = effort if effort in {"low", "high", "max"} else "high"
        elif profile.protocol == "qwen":
            effective_thinking = str(settings.get("effective_thinking", "auto")).strip().lower()
            if effective_thinking == "off":
                payload["enable_thinking"] = False
            elif self.model_name.strip().lower().startswith("qwen3.8"):
                effort = {"low": "low", "high": "xhigh", "max": "xhigh"}.get(effective_thinking, "xhigh")
                payload["reasoning_effort"] = effort
            else:
                payload["enable_thinking"] = True
        elif profile.protocol == "glm":
            effective_thinking = str(settings.get("effective_thinking", "auto")).strip().lower()
            payload["thinking"] = {"type": "disabled" if effective_thinking == "off" else "enabled"}
            if payload["thinking"]["type"] == "enabled":
                effort = str(settings.get("reasoning_effort", effective_thinking if effective_thinking != "auto" else "max")).strip().lower()
                payload["reasoning_effort"] = effort if effort in {"low", "high", "max"} else "max"

        response = await self._request(
            payload,
            connect_timeout=_positive_float(settings.get("connect_timeout"), 10),
            read_timeout=_positive_float(settings.get("read_timeout"), 120),
            write_timeout=_positive_float(settings.get("write_timeout"), 30),
            pool_timeout=_positive_float(settings.get("pool_timeout"), 10),
        )
        choices = response.get("choices")
        if not isinstance(choices, list) or not choices:
            raise LLMError("Provider returned no choices", retryable=True)
        choice = choices[0] if isinstance(choices[0], dict) else {}
        message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
        text = _content_to_text(message.get("content"))
        reasoning_content = _content_to_text(message.get("reasoning_content")) or None
        usage = response.get("usage") if isinstance(response.get("usage"), dict) else {}
        return LLMResponse(
            text=text,
            input_tokens=int(usage.get("prompt_tokens", 0)),
            output_tokens=int(usage.get("completion_tokens", 0)),
            model=self.model_name,
            finish_reason=str(choice.get("finish_reason")) if choice.get("finish_reason") is not None else None,
            message_content=text,
            reasoning_content=reasoning_content,
            usage=dict(usage),
            request_parameters=_request_diagnostics(payload, self.provider_name),
        )

    async def _request(
        self,
        payload: dict[str, Any],
        *,
        connect_timeout: float,
        read_timeout: float,
        write_timeout: float,
        pool_timeout: float,
    ) -> dict[str, Any]:
        timeout = httpx.Timeout(
            connect=connect_timeout,
            read=read_timeout,
            write=write_timeout,
            pool=pool_timeout,
        )
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                # Keep the HTTP operation in its own task so cancellation waits for
                # httpx to finish closing the request before a retry can begin.
                request_task = asyncio.create_task(client.post(
                    f"{self.base_url}/chat/completions",
                    json=payload,
                    headers={"Authorization": f"Bearer {self.api_key}"},
                ))
                try:
                    response = await request_task
                except asyncio.CancelledError:
                    if not request_task.done():
                        request_task.cancel()
                    await asyncio.gather(request_task, return_exceptions=True)
                    raise
        except httpx.ConnectTimeout as exc:
            raise LLMTimeoutError(f"Provider connect timeout after {connect_timeout:g}s") from exc
        except httpx.ReadTimeout as exc:
            raise LLMTimeoutError(f"Provider read timeout after {read_timeout:g}s") from exc
        except httpx.WriteTimeout as exc:
            raise LLMTimeoutError(f"Provider write timeout after {write_timeout:g}s") from exc
        except httpx.PoolTimeout as exc:
            raise LLMTimeoutError(f"Provider connection pool timeout after {pool_timeout:g}s") from exc
        except httpx.RequestError as exc:
            raise LLMError(f"Provider network error: {exc}", retryable=True) from exc

        if response.is_error:
            detail = _response_detail(response, self.api_key)
            retryable = response.status_code == 408 or response.status_code == 429 or response.status_code >= 500
            suffix = f": {detail}" if detail else ""
            raise LLMError(f"Provider returned HTTP {response.status_code}{suffix}", retryable=retryable)

        try:
            data = response.json()
        except ValueError as exc:
            raise LLMError("Provider returned invalid JSON", retryable=True) from exc
        if not isinstance(data, dict):
            raise LLMError("Provider returned an invalid response object", retryable=True)
        return data


def _positive_float(value: Any, default: float) -> float:
    try:
        return max(0.1, float(value))
    except (TypeError, ValueError):
        return default


def _request_diagnostics(payload: dict[str, Any], provider_name: str) -> dict[str, Any]:
    """Return the exact non-secret controls sent to the cloud endpoint."""
    safe_keys = ("model", "max_tokens", "enable_thinking", "reasoning_effort", "thinking")
    result = {key: payload[key] for key in safe_keys if key in payload}
    result["provider"] = provider_name
    return result


def _positive_int(value: Any, default: int) -> int:
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return default


def _content_to_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "".join(parts)
    return str(value)


def _response_detail(response: httpx.Response, api_key: str) -> str:
    try:
        data = response.json()
        error = data.get("error") if isinstance(data, dict) else None
        if isinstance(error, dict):
            detail = error.get("message") or error.get("type") or ""
        else:
            detail = data.get("message", "") if isinstance(data, dict) else ""
    except ValueError:
        detail = response.text.strip()
    sanitized = str(detail).replace(api_key, "[redacted]").replace("\r", " ").replace("\n", " ").strip()
    return sanitized[:300]
