"""Provider request lifecycle shared by every workflow Agent."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from ..llm.base import LLMProvider, LLMResponse, LLMTimeoutError
from ..repositories.sqlite import SQLiteRepository


class ModelInvocationService:
    """Own transport timeout, cancellation, and the durable attempt ledger."""

    def __init__(self, provider: LLMProvider, repository: SQLiteRepository) -> None:
        self.provider = provider
        self.repository = repository

    async def generate(
        self,
        system_prompt: str,
        user_prompt: str,
        config: dict[str, Any],
        timeout_seconds: float,
    ) -> LLMResponse:
        request_task = asyncio.create_task(
            self.provider.generate(system_prompt, user_prompt, config)
        )
        try:
            return await asyncio.wait_for(request_task, timeout=timeout_seconds)
        except asyncio.TimeoutError as exc:
            await self.cancel_and_wait(request_task)
            raise LLMTimeoutError(
                f"Agent timed out after {timeout_seconds:g}s"
            ) from exc
        except asyncio.CancelledError:
            await self.cancel_and_wait(request_task)
            raise

    @staticmethod
    async def cancel_and_wait(task: asyncio.Task[Any]) -> None:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if not task.done():
            raise RuntimeError("Provider request did not finish cancellation cleanup")

    def persist_attempts(
        self,
        run_id: str,
        step_id: str,
        attempts: list[dict[str, Any]],
    ) -> None:
        if not attempts:
            return
        latest = attempts[-1]
        self.repository.upsert_step(
            run_id,
            step_id,
            finish_reason=latest.get("finish_reason"),
            message_content=latest.get("message_content"),
            reasoning_content=latest.get("reasoning_content"),
            usage_json=json.dumps(
                latest.get("usage", {}), ensure_ascii=False, default=str
            ),
            provider_attempts_json=json.dumps(
                attempts, ensure_ascii=False, default=str
            ),
        )
        self.repository.record_provider_attempts(run_id, step_id, attempts)

