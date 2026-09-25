"""Provider request lifecycle shared by every workflow Agent."""

from __future__ import annotations

import asyncio
import contextvars
import json
from contextlib import contextmanager
from typing import Any

from ..llm.base import LLMProvider, LLMResponse, LLMTimeoutError
from ..repositories.sqlite import SQLiteRepository


class ModelInvocationService:
    """Own transport timeout, cancellation, and the durable attempt ledger."""

    def __init__(self, provider: LLMProvider, repository: SQLiteRepository) -> None:
        self.provider = provider
        self.repository = repository
        self._run_providers: dict[str, LLMProvider] = {}
        self._current_run: contextvars.ContextVar[str | None] = contextvars.ContextVar(
            f"model_invocation_run_{id(self)}", default=None
        )

    def set_provider(self, provider: LLMProvider) -> None:
        """Change the provider for future Runs without mutating active snapshots."""
        self.provider = provider

    def capture_run(self, run_id: str) -> None:
        if run_id:
            self._run_providers.setdefault(run_id, self.provider)

    def release_run(self, run_id: str, *, keep: bool = False) -> None:
        if not keep:
            self._run_providers.pop(run_id, None)

    @contextmanager
    def run_scope(self, run_id: str):
        self.capture_run(run_id)
        token = self._current_run.set(run_id)
        try:
            yield
        finally:
            self._current_run.reset(token)

    def _provider_for_current_run(self) -> LLMProvider:
        run_id = self._current_run.get()
        return self._run_providers.get(run_id, self.provider) if run_id else self.provider

    def provider_for_run(self, run_id: str) -> LLMProvider:
        return self._run_providers.get(run_id, self.provider)

    def current_provider(self) -> LLMProvider:
        return self._provider_for_current_run()

    async def generate(
        self,
        system_prompt: str,
        user_prompt: str,
        config: dict[str, Any],
        timeout_seconds: float,
    ) -> LLMResponse:
        provider = self._provider_for_current_run()
        request_task = asyncio.create_task(provider.generate(system_prompt, user_prompt, config))
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
