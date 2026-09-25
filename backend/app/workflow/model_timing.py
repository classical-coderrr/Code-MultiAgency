"""Shared model-request timing instrumentation for workflow execution."""
from __future__ import annotations

import time
from typing import Any


async def generate_with_timing(
    model_invocation: Any,
    event_bus: Any,
    run_id: str,
    step_id: str,
    system: str,
    user: str,
    config: dict[str, Any],
    timeout: float,
    *,
    attempt: int | None = None,
) -> Any:
    started = time.perf_counter()
    payload = {
        "stepId": step_id,
        "phase": str(config.get("generation_phase") or "agent_step"),
        "fileName": str(config.get("artifact_name") or ""),
        "attempt": max(1, attempt or int(config.get("continuation_attempt", 0) or 0) + 1),
    }
    try:
        response = await model_invocation.generate(system, user, config, timeout)
    except Exception as exc:
        await event_bus.emit("step.model_request_completed", run_id, {
            **payload,
            "status": "failed",
            "durationMs": max(0, int((time.perf_counter() - started) * 1000)),
            "errorType": type(exc).__name__,
        })
        raise
    await event_bus.emit("step.model_request_completed", run_id, {
        **payload,
        "status": "completed",
        "durationMs": max(0, int((time.perf_counter() - started) * 1000)),
        "inputTokens": max(0, int(response.input_tokens or 0)),
        "outputTokens": max(0, int(response.output_tokens or 0)),
        "finishReason": str(response.finish_reason or ""),
    })
    return response
