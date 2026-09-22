"""Deterministic local provider used for demos and tests."""

from __future__ import annotations

import asyncio
import json
from html import escape
from typing import Any

from .base import LLMProvider, LLMResponse


class MockProvider(LLMProvider):
    def capabilities(self) -> dict[str, Any]:
        return {
            "provider": "mock",
            "model": "mock",
            "supportsThinking": True,
            "supportedLevels": ["off", "low", "high", "max"],
            "defaultLevel": "low",
            "maxTokens": {"min": 1, "max": 6000},
        }

    async def generate(
        self,
        system_prompt: str,
        user_prompt: str,
        config: dict[str, Any] | None = None,
    ) -> LLMResponse:
        await asyncio.sleep(0.25)
        agent_id = (config or {}).get("agent_id", "agent")
        request_excerpt = _request_excerpt(user_prompt)
        generation_phase = str((config or {}).get("generation_phase", "")).strip().lower()
        if generation_phase == "artifact_plan":
            return _artifact_plan_response(request_excerpt)
        if generation_phase in {"artifact_file", "artifact_repair"}:
            return _artifact_file_response(str((config or {}).get("artifact_name", "artifact.txt")), request_excerpt)
        detection_text = request_excerpt.lower().rsplit("当前任务摘要：", 1)[-1]
        static_html = any(
            token in detection_text
            for token in ("static_html", "html", "静态", "广告页", "单页面")
        )
        architecture = json.dumps(
            {
                "project_type": "static_html" if static_html else "web_app",
                "backend_required": not static_html,
                "complexity": "low" if static_html else "medium",
                "summary": f"当前任务：{request_excerpt}",
                "frontend_plan": ["语义化 HTML", "响应式 CSS", "必要的轻量交互"],
                "backend_reason": "静态页面不需要 API、数据库或登录。" if static_html else "应用需要保留后端接口与数据边界。",
                "risks": ["核对页面布局", "验证移动端显示"],
            },
            ensure_ascii=False,
        )
        budget_plan = json.dumps(
            {
                "difficulty": "low" if static_html else "medium",
                "model_strength": "standard",
                "confidence": 0.85,
                "agent_budgets": {
                    "requirement": 1800 if static_html else 3000,
                    "architecture": 2200 if static_html else 3600,
                    "backend": 1000 if static_html else 4000,
                    "frontend": 3600 if static_html else 4800,
                    "tester": 1800 if static_html else 2600,
                    "reviewer": 1600 if static_html else 2400,
                },
                "thinking": {
                    "requirement": "low",
                    "architecture": "low",
                    "backend": "off",
                    "frontend": "off",
                    "tester": "low",
                    "reviewer": "low",
                },
                "rationale": ["按需求范围估算", "为最终可见内容保留输出空间"],
            },
            ensure_ascii=False,
        )
        if static_html:
            frontend_output = (
                "<!doctype html>\n"
                "<html lang=\"zh-CN\"><head><meta charset=\"UTF-8\">"
                "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\">"
                "<title>Agent 生成页面</title>"
                "<style>body{font-family:Arial,sans-serif;margin:0;padding:48px;background:#f5f7fb;color:#172033}"
                "main{max-width:760px;margin:auto;padding:48px;border-radius:24px;background:white;"
                "box-shadow:0 16px 50px #17203318}h1{font-size:42px;margin:0 0 16px}"
                "p{line-height:1.7;color:#5a6578}</style></head>"
                f"<body><main><h1>当前需求预览</h1><p>{escape(request_excerpt[:240])}</p>"
                "</main></body></html>"
            )
        else:
            frontend_output = f"前端方案已根据当前需求生成：{request_excerpt}"

        outputs = {
            "token_budget_agent": budget_plan,
            "requirement_agent": f"需求分析已完成。\n当前任务摘要：{request_excerpt}\n已提取目标、范围、角色和验收关注点。",
            "architect_agent": architecture,
            "backend_agent": f"后端方案已根据当前需求生成：{request_excerpt}\n将补充 API、数据边界、错误处理和可观测性。",
            "frontend_agent": frontend_output,
            "tester_agent": f"测试方案已根据当前需求生成：{request_excerpt}\n将覆盖主流程、异常路径、边界输入和回归验证。",
            "reviewer_agent": f"最终审查已针对当前需求完成：{request_excerpt}\n方案将以验收标准、风险和后续交付物作为结论。",
        }
        text = outputs.get(agent_id, f"{agent_id} 已完成分析。\n\n输入摘要：{request_excerpt}")
        return LLMResponse(
            text=text,
            input_tokens=max(1, len(user_prompt) // 4),
            output_tokens=max(1, len(text) // 4),
            finish_reason="stop",
            message_content=text,
        )


def _request_excerpt(user_prompt: str) -> str:
    """Keep demo output tied to the current request without echoing huge prompts."""
    compact = " ".join(user_prompt.split())
    return compact[-600:] if compact else "未提供需求"


def _artifact_plan_response(request_excerpt: str) -> LLMResponse:
    is_html = any(token in request_excerpt.lower() for token in ("html", "网页", "网站", "广告页", "静态"))
    files = [{"name": "index.html", "language": "html", "purpose": "页面结构与交互入口", "estimated_tokens": 1400}] if is_html else [
        {"name": "main.py", "language": "python", "purpose": "后端入口", "estimated_tokens": 900},
        {"name": "README.md", "language": "markdown", "purpose": "运行说明", "estimated_tokens": 500},
    ]
    text = json.dumps({"files": files}, ensure_ascii=False)
    return LLMResponse(text=text, input_tokens=max(1, len(request_excerpt) // 4), output_tokens=max(1, len(text) // 4), finish_reason="stop", message_content=text)


def _artifact_file_response(name: str, request_excerpt: str) -> LLMResponse:
    suffix = name.lower().rsplit(".", 1)[-1] if "." in name else "txt"
    if suffix in {"html", "htm"}:
        text = (
            "<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"UTF-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\">"
            "<title>Agent 生成页面</title></head><body><main>"
            f"<h1>当前需求预览</h1><p>{escape(request_excerpt[:240])}</p>"
            "</main></body></html>"
        )
    elif suffix == "css":
        text = "body { font-family: sans-serif; margin: 0; }"
    elif suffix in {"js", "mjs"}:
        text = "export function init() { return true; }"
    elif suffix == "py":
        text = "def main():\n    return True\n\n\nif __name__ == \"__main__\":\n    main()"
    elif suffix == "md":
        text = f"# 运行说明\n\n本文件对应当前需求：{request_excerpt[:240]}"
    elif suffix == "sql":
        text = (
            "CREATE TABLE IF NOT EXISTS student (\n"
            "  id BIGINT PRIMARY KEY,\n"
            "  name VARCHAR(100) NOT NULL,\n"
            "  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP\n"
            ");"
        )
    elif suffix == "json":
        text = "{}"
    else:
        text = f"Generated artifact: {name}"
    return LLMResponse(text=text, input_tokens=max(1, len(request_excerpt) // 4), output_tokens=max(1, len(text) // 4), finish_reason="stop", message_content=text)
