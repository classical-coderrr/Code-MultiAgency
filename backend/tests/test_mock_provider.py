import asyncio
import json

from app.llm.mock import MockProvider


def test_mock_provider_uses_the_current_request_for_each_run():
    provider = MockProvider()

    first = asyncio.run(
        provider.generate(
            "system",
            "请开发一个咖啡店品牌官网，需求重点是门店介绍和预约。",
            {"agent_id": "requirement_agent"},
        )
    )
    second = asyncio.run(
        provider.generate(
            "system",
            "请开发一个库存管理后台，需求重点是入库、出库和库存预警。",
            {"agent_id": "requirement_agent"},
        )
    )

    assert "咖啡店品牌官网" in first.text
    assert "库存管理后台" in second.text
    assert first.text != second.text


def test_mock_architecture_detects_static_html_from_current_request():
    response = asyncio.run(
        MockProvider().generate(
            "system",
            "当前任务摘要：开发一个简单的 HTML 广告页。",
            {"agent_id": "architect_agent"},
        )
    )

    architecture = json.loads(response.text)
    assert architecture["project_type"] == "static_html"
    assert architecture["backend_required"] is False
