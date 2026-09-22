"""In-process event bus backing the SSE endpoint."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, AsyncIterator

from ..repositories.sqlite import SQLiteRepository
from ..runtime.coordination import RunCoordinator


TERMINAL_EVENTS = {"workflow.completed", "workflow.failed", "workflow.stopped"}


class WorkflowEventBus:
    def __init__(
        self,
        repository: SQLiteRepository | None = None,
        coordinator: RunCoordinator | None = None,
    ) -> None:
        self.repository = repository
        self.coordinator = coordinator
        self._queues: dict[str, set[asyncio.Queue[dict[str, Any]]]] = defaultdict(set)
        self._history: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self._sequence = 0
        self.coordination_error: str | None = None

    async def emit(self, event_type: str, run_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        timestamp = datetime.now(timezone.utc).isoformat()
        if self.repository:
            event_id = self.repository.append_event(run_id, event_type, timestamp, payload or {})
        else:
            self._sequence += 1
            event_id = self._sequence
        event = {
            "id": event_id,
            "type": event_type,
            "runId": run_id,
            "timestamp": timestamp,
            "payload": payload or {},
        }
        self._remember(run_id, event)
        if self.coordinator is not None:
            try:
                await self.coordinator.publish_event(event)
                self.coordination_error = None
            except Exception as exc:
                # SQL is authoritative; Redis fan-out may degrade without
                # turning a successful Agent operation into a Run failure.
                self.coordination_error = str(exc)
        for queue in list(self._queues.get(run_id, set())):
            await queue.put(event)
        return event

    def _remember(self, run_id: str, event: dict[str, Any]) -> None:
        if any(item.get("id") == event.get("id") for item in self._history[run_id]):
            return
        self._history[run_id].append(event)

    def _load_history(self, run_id: str, after_event_id: int = 0) -> list[dict[str, Any]]:
        if not self.repository:
            return [event for event in self._history.get(run_id, []) if int(event.get("id") or 0) > after_event_id]
        for event in self.repository.list_events(run_id, after_event_id):
            self._remember(run_id, event)
        return [event for event in self._history.get(run_id, []) if int(event.get("id") or 0) > after_event_id]

    def _is_terminal_run(self, run_id: str) -> bool:
        """Check the durable Run status before closing a replay stream.

        A failed Run can be moved back to RUNNING by the retry endpoint before
        the new recovery events are emitted.  Looking only at the last replayed
        event would close the new SSE connection on the old ``workflow.failed``
        event and lose the recovery stream.
        """
        if not self.repository:
            return False
        run = self.repository.get_run(run_id)
        return bool(run and run.get("status") in {"SUCCESS", "FAILED", "STOPPED"})

    async def stream(self, run_id: str, after_event_id: int = 0) -> AsyncIterator[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._queues[run_id].add(queue)
        history = self._load_history(run_id, after_event_id)
        seen_ids = {event.get("id") for event in history}
        last_seen = max([after_event_id, *[int(event.get("id") or 0) for event in history]])
        for event in history:
            yield event
        history_is_terminal = bool(history and history[-1].get("type") in TERMINAL_EVENTS)
        if self._is_terminal_run(run_id) or (self.repository is None and history_is_terminal):
            self._queues[run_id].discard(queue)
            return

        try:
            if self.coordinator is not None:
                try:
                    async for event in self.coordinator.stream_events(run_id, last_seen):
                        event_id = int(event.get("id") or 0)
                        if event_id in seen_ids or event_id <= last_seen:
                            continue
                        seen_ids.add(event_id)
                        last_seen = event_id
                        self._remember(run_id, event)
                        yield event
                        if event.get("type") in TERMINAL_EVENTS and self._is_terminal_run(run_id):
                            return
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self.coordination_error = str(exc)
                    # Redis may be temporarily unavailable. Poll the durable
                    # SQL outbox until live coordination recovers or the Run
                    # reaches a terminal state.
                    while True:
                        fresh = self.repository.list_events(run_id, last_seen) if self.repository else []
                        for event in fresh:
                            event_id = int(event.get("id") or 0)
                            last_seen = max(last_seen, event_id)
                            yield event
                        if self._is_terminal_run(run_id):
                            return
                        await asyncio.sleep(1)
            while True:
                event = await queue.get()
                event_id = int(event.get("id") or 0)
                if event_id in seen_ids or event_id <= last_seen:
                    continue
                seen_ids.add(event_id)
                last_seen = event_id
                yield event
                if event["type"] in TERMINAL_EVENTS and (
                    self.repository is None or self._is_terminal_run(run_id)
                ):
                    return
        finally:
            self._queues[run_id].discard(queue)
            if not self._queues[run_id]:
                self._queues.pop(run_id, None)
