from app.llm.factory import ProviderFactory
from app.llm.local_codex import LocalCodexProvider, _parse_jsonl
from app.llm.openai_compatible import OpenAICompatibleProvider


class FixtureProvider(OpenAICompatibleProvider):
    async def _request(self, payload, *, connect_timeout, read_timeout, write_timeout, pool_timeout):
        return {
            "choices": [{
                "finish_reason": "stop",
                "message": {
                    "content": "final answer",
                    "reasoning_content": "internal reasoning",
                },
            }],
            "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
        }


def test_deepseek_provider_uses_openai_compatible_transport():
    provider = ProviderFactory.create(
        "deepseek",
        "https://api.deepseek.com",
        "test-key",
        "deepseek-chat",
    )

    assert isinstance(provider, OpenAICompatibleProvider)


def test_qwen_provider_uses_openai_compatible_transport_without_deepseek_payload_mode():
    provider = ProviderFactory.create(
        "qwen",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "test-key",
        "qwen-plus",
    )

    assert isinstance(provider, OpenAICompatibleProvider)
    assert provider.provider_name == "qwen"
    assert provider.resolve_thinking("high") == ("off", "This model does not support thinking controls; thinking was turned off.")


def test_qwen_accepts_sk_ws_key_format():
    provider = ProviderFactory.create(
        "qwen",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "sk-ws-test-key",
        "qwen-plus",
    )

    assert isinstance(provider, OpenAICompatibleProvider)
    assert provider.api_key == "sk-ws-test-key"


def test_qwen38_supports_thinking_levels_and_maps_auto_to_xhigh():
    provider = ProviderFactory.create(
        "qwen",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "test-key",
        "qwen3.8-max",
    )

    capabilities = provider.capabilities()
    assert capabilities["supportsThinking"] is True
    assert capabilities["supportedLevels"] == ["off", "low", "high", "max"]
    assert capabilities["defaultLevel"] == "max"
    assert provider.resolve_thinking("low") == ("low", None)
    assert provider.resolve_thinking("high") == ("high", None)
    assert provider.resolve_thinking("auto") == ("max", None)


class RecordingProvider(OpenAICompatibleProvider):
    async def _request(self, payload, *, connect_timeout, read_timeout, write_timeout, pool_timeout):
        self.payload = payload
        return {
            "choices": [{"finish_reason": "stop", "message": {"content": "ok"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }


def test_qwen38_payload_uses_off_switch_and_reasoning_effort_mapping():
    import asyncio

    provider = RecordingProvider(
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "test-key",
        "qwen3.8-max",
        "qwen",
    )
    asyncio.run(provider.generate("system", "user", {"effective_thinking": "low"}))
    assert provider.payload["reasoning_effort"] == "low"
    assert "enable_thinking" not in provider.payload

    asyncio.run(provider.generate("system", "user", {"effective_thinking": "off"}))
    assert provider.payload["enable_thinking"] is False
    assert "reasoning_effort" not in provider.payload


def test_dashscope_deepseek_v4_uses_model_family_capabilities_and_controls():
    import asyncio

    provider = RecordingProvider(
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "test-key",
        "deepseek-v4-pro-0813",
        "qwen",
    )

    capabilities = provider.capabilities()
    assert capabilities["transport"] == "dashscope_openai_compatible"
    assert capabilities["modelFamily"] == "deepseek_v4"
    assert capabilities["supportsThinking"] is True
    assert provider.resolve_thinking("auto") == ("high", None)

    response = asyncio.run(provider.generate("system", "user", {"effective_thinking": "low"}))
    assert provider.payload["enable_thinking"] is True
    assert provider.payload["reasoning_effort"] == "low"
    assert response.request_parameters["enable_thinking"] is True
    assert response.request_parameters["reasoning_effort"] == "low"

    response = asyncio.run(provider.generate("system", "user", {"effective_thinking": "off"}))
    assert provider.payload["enable_thinking"] is False
    assert "reasoning_effort" not in provider.payload
    assert response.provider_record()["request_parameters"]["enable_thinking"] is False


def test_deepseek_auto_resolves_to_native_default():
    provider = ProviderFactory.create("deepseek", "https://api.deepseek.com", "test-key", "deepseek-chat")

    assert provider.resolve_thinking("auto") == ("high", None)


def test_unknown_openai_compatible_model_downgrades_thinking():
    provider = ProviderFactory.create("openai_compatible", "https://example.com", "test-key", "chat-model")

    effective, warning = provider.resolve_thinking("max")
    assert effective == "off"
    assert warning


def test_glm52_uses_model_family_profile_even_with_generic_provider():
    provider = ProviderFactory.create(
        "openai_compatible",
        "https://open.bigmodel.cn/api/paas/v4",
        "test-key",
        "glm-5.2",
    )

    capabilities = provider.capabilities()
    assert capabilities["modelFamily"] == "glm"
    assert capabilities["supportsThinking"] is True
    assert capabilities["defaultLevel"] == "max"
    assert provider.resolve_thinking("low") == ("low", None)


def test_glm52_warns_when_paired_with_dashscope_base_url():
    provider = ProviderFactory.create(
        "qwen",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "test-key",
        "glm-5.2",
    )

    capabilities = provider.capabilities()
    assert capabilities["modelFamily"] == "glm"
    assert "configurationWarning" in capabilities


def test_glm52_payload_uses_native_thinking_contract():
    import asyncio

    provider = RecordingProvider(
        "https://open.bigmodel.cn/api/paas/v4",
        "test-key",
        "glm-5.2",
        "openai_compatible",
    )

    asyncio.run(provider.generate("system", "user", {"effective_thinking": "off"}))
    assert provider.payload["thinking"] == {"type": "disabled"}
    assert "reasoning_effort" not in provider.payload

    asyncio.run(provider.generate("system", "user", {"effective_thinking": "low"}))
    assert provider.payload["thinking"] == {"type": "enabled"}
    assert provider.payload["reasoning_effort"] == "low"


def test_openai_compatible_response_records_provider_fields():
    provider = FixtureProvider("https://example.com", "test-key", "test-model")

    import asyncio

    response = asyncio.run(provider.generate("system", "user"))

    assert response.text == "final answer"
    assert response.finish_reason == "stop"
    assert response.message_content == "final answer"
    assert response.reasoning_content == "internal reasoning"
    assert response.usage["total_tokens"] == 18


def test_chatgpt_local_provider_uses_local_codex_runner_without_api_key():
    provider = ProviderFactory.create("chatgpt_local", "", "", "codex-default")

    assert isinstance(provider, LocalCodexProvider)
    capabilities = provider.capabilities()
    assert capabilities["transport"] == "codex_cli"
    assert capabilities["authentication"] == "local_login"
    assert capabilities["supportsThinking"] is False


def test_local_codex_jsonl_parser_extracts_message_reasoning_and_usage():
    raw = b"\n".join(
        [
            b'{"type":"item.completed","item":{"type":"reasoning","text":"plan"}}',
            b'{"type":"item.completed","item":{"type":"agent_message","text":"result"}}',
            b'{"type":"turn.completed","usage":{"input_tokens":12,"output_tokens":8},"status":"completed"}',
        ]
    )

    text, reasoning, usage, finish_reason, error = _parse_jsonl(raw)

    assert text == "result"
    assert reasoning == "plan"
    assert usage == {"input_tokens": 12, "output_tokens": 8}
    assert finish_reason == "completed"
    assert error == ""
