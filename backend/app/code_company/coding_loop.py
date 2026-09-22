"""Provider-neutral Plan -> Act -> Observe -> Repair -> Verify loop."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from ..llm.base import LLMProvider, LLMResponse
from ..platform.tool_gateway import LocalToolGateway, ToolCall, ToolResult
from ..platform.workspace import WorkspaceRef


Emit = Callable[[str, dict[str, Any]], Awaitable[None]] | None


@dataclass(frozen=True, slots=True)
class CodingLoopConfig:
    max_iterations: int = 8
    timeout_seconds: float = 120.0
    max_tokens: int = 4000
    thinking: str = "low"


@dataclass(slots=True)
class CodingLoopResult:
    output: str
    responses: list[LLMResponse] = field(default_factory=list)
    tool_results: list[ToolResult] = field(default_factory=list)
    iterations: int = 0
    verified: bool = False


class CodingAgentLoop:
    def __init__(self, provider: LLMProvider, gateway: LocalToolGateway) -> None:
        self.provider = provider
        self.gateway = gateway

    async def run(
        self,
        workspace: WorkspaceRef | dict[str, Any],
        task: str,
        *,
        system_prompt: str = "",
        config: CodingLoopConfig | None = None,
        emit: Emit = None,
    ) -> CodingLoopResult:
        settings = config or CodingLoopConfig()
        history: list[dict[str, Any]] = []
        responses: list[LLMResponse] = []
        tool_results: list[ToolResult] = []
        final_output = ""
        verified = False
        for iteration in range(1, max(1, settings.max_iterations) + 1):
            prompt = self._prompt(task, history, iteration, settings.max_iterations)
            if emit:
                await emit("coding_loop.iteration_started", {"iteration": iteration})
            response = await asyncio.wait_for(
                self.provider.generate(
                    system_prompt or self._system_prompt(),
                    prompt,
                    {
                        "agent_id": "coding_agent_loop",
                        "max_tokens": settings.max_tokens,
                        "effective_thinking": settings.thinking,
                        "reasoning_effort": settings.thinking,
                        "request_timeout": settings.timeout_seconds,
                    },
                ),
                timeout=settings.timeout_seconds,
            )
            responses.append(response)
            instruction = self._parse(response.text)
            if not instruction:
                final_output = response.text.strip()
                history.append({"type": "assistant", "content": final_output})
                break
            if instruction.get("type") == "final":
                final_output = str(instruction.get("content") or "")
                verified = bool(instruction.get("verified", False))
                history.append(instruction)
                break
            calls = instruction.get("actions") if isinstance(instruction.get("actions"), list) else [instruction]
            observations: list[dict[str, Any]] = []
            for action in calls:
                if not isinstance(action, dict) or not action.get("tool"):
                    continue
                call = ToolCall(str(action["tool"]), action.get("arguments") if isinstance(action.get("arguments"), dict) else {})
                result = await self.gateway.aexecute(workspace, call)
                tool_results.append(result)
                observations.append(result.as_dict())
                if emit:
                    await emit("coding_loop.tool_result", {"iteration": iteration, **result.as_dict()})
            history.append({"type": "assistant", "instruction": instruction, "observations": observations})
            if emit:
                await emit("coding_loop.iteration_completed", {"iteration": iteration, "toolCount": len(observations)})
        if not final_output:
            final_output = "Coding loop reached its iteration limit without a final response."
        return CodingLoopResult(final_output, responses, tool_results, len(responses), verified)

    @staticmethod
    def _system_prompt() -> str:
        return (
            "You are a coding agent operating through a restricted Tool Gateway. "
            "Return JSON only. Use {\"type\":\"tool_call\",\"tool\":\"repo.tree|repo.search|repo.read|fs.create|fs.write|fs.patch|fs.delete|build.run|test.run\",\"arguments\":{}} "
            "for work, or {\"type\":\"final\",\"content\":\"...\",\"verified\":true} when finished. "
            "Never invent command output; inspect, edit, run, observe, repair, then verify."
        )

    @classmethod
    def _prompt(cls, task: str, history: list[dict[str, Any]], iteration: int, max_iterations: int) -> str:
        compact_history = json.dumps(history[-6:], ensure_ascii=False, separators=(",", ":"))
        return f"Task:\n{task}\nIteration: {iteration}/{max_iterations}\nRecent tool history:\n{compact_history}"

    @staticmethod
    def _parse(value: str) -> dict[str, Any] | None:
        text = str(value or "").strip()
        if text.startswith("```"):
            lines = text.splitlines()
            text = "\n".join(lines[1:-1]).strip()
        try:
            parsed = json.loads(text)
        except (TypeError, ValueError):
            return None
        return parsed if isinstance(parsed, dict) else None
