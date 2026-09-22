"""Provider construction. Configuration stays outside the executor."""

from __future__ import annotations

import os

from .base import LLMProvider
from .local_codex import LocalCodexProvider
from .mock import MockProvider
from .openai_compatible import OpenAICompatibleProvider


class ProviderFactory:
    @staticmethod
    def create(provider_name: str, base_url: str, api_key: str, model_name: str) -> LLMProvider:
        provider = provider_name.strip().lower()
        if provider in {"chatgpt_local", "codex_local"}:
            return LocalCodexProvider(
                model_name or "codex-default",
                executable=os.getenv("CODEX_EXECUTABLE", "codex.exe"),
                working_directory=os.getenv("CODEX_WORKDIR") or None,
            )

        placeholder_key = not api_key or api_key.startswith("REPLACE_WITH_") or api_key.startswith("sk-your-")
        if base_url.strip() and model_name.strip() and not placeholder_key:
            return OpenAICompatibleProvider(base_url.strip(), api_key.strip(), model_name.strip(), provider)
        return MockProvider()

    @staticmethod
    def create_from_env() -> LLMProvider:
        provider_name = os.getenv("MODEL_PROVIDER", "qwen").strip().lower()
        base_url = os.getenv("MODEL_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
        api_key = os.getenv("MODEL_API_KEY")
        model_name = os.getenv("MODEL_NAME", "qwen-plus")
        # 占位 Key 不会触发外部请求，避免新环境启动时误把示例字符串发给云端。
        return ProviderFactory.create(provider_name, base_url or "", api_key or "", model_name or "")
