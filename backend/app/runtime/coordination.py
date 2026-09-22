"""Redis-backed queue, lease, control, and event-stream coordination.

SQLite/PostgreSQL remains the durable source of truth. Redis is deliberately
used only for transient coordination so a Redis outage cannot erase Run
history, checkpoints, or artifacts.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from typing import Any, AsyncIterator, Protocol


class RunCoordinator(Protocol):
    async def ping(self) -> bool: ...
    async def close(self) -> None: ...
    async def publish_event(self, event: dict[str, Any]) -> None: ...
    async def stream_events(self, run_id: str, after_event_id: int = 0) -> AsyncIterator[dict[str, Any]]: ...
    async def enqueue(self, run_id: str, action: str, payload: dict[str, Any] | None = None) -> str: ...
    async def claim_job(self, worker_id: str, *, block_ms: int = 5000) -> "ClaimedJob | None": ...
    async def ack_job(self, message_id: str) -> None: ...
    async def acquire_lease(self, run_id: str, worker_id: str, ttl_seconds: int) -> "RunLease | None": ...
    async def renew_lease(self, lease: "RunLease", ttl_seconds: int) -> bool: ...
    async def release_lease(self, lease: "RunLease") -> bool: ...
    async def lease_owner(self, run_id: str) -> dict[str, Any] | None: ...
    async def send_control(self, run_id: str, action: str, payload: dict[str, Any] | None = None) -> str: ...
    async def read_controls(self, run_id: str, after_id: str, *, block_ms: int = 1000) -> tuple[str, list[dict[str, Any]]]: ...
    async def heartbeat_worker(self, worker_id: str, ttl_seconds: int = 30) -> None: ...
    async def remove_worker(self, worker_id: str) -> None: ...
    async def active_workers(self) -> list[dict[str, Any]]: ...


@dataclass(frozen=True)
class ClaimedJob:
    message_id: str
    run_id: str
    action: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class RunLease:
    run_id: str
    worker_id: str
    token: str


def _decode(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


class RedisRunCoordinator:
    """Small Redis Streams adapter with fenced, renewable Run leases."""

    _renew_script = """
    local current = redis.call('GET', KEYS[1])
    if not current then return 0 end
    local decoded = cjson.decode(current)
    if decoded['token'] ~= ARGV[1] then return 0 end
    redis.call('PEXPIRE', KEYS[1], ARGV[2])
    return 1
    """
    _release_script = """
    local current = redis.call('GET', KEYS[1])
    if not current then return 0 end
    local decoded = cjson.decode(current)
    if decoded['token'] ~= ARGV[1] then return 0 end
    return redis.call('DEL', KEYS[1])
    """

    def __init__(
        self,
        url: str,
        *,
        prefix: str = "agent-team",
        worker_group: str = "agent-team-workers",
        reclaim_idle_ms: int = 30_000,
    ) -> None:
        try:
            from redis.asyncio import Redis
        except ImportError as exc:  # pragma: no cover - depends on deployment extras
            raise RuntimeError("Redis coordination requires the 'redis' Python package") from exc
        self.url = url
        self.prefix = prefix.strip(":") or "agent-team"
        self.worker_group = worker_group
        self.reclaim_idle_ms = max(5_000, int(reclaim_idle_ms))
        self.redis = Redis.from_url(url, decode_responses=False, health_check_interval=15)
        self._group_ready = False

    @property
    def jobs_key(self) -> str:
        return f"{self.prefix}:jobs"

    def _events_key(self, run_id: str) -> str:
        return f"{self.prefix}:run:{run_id}:events"

    def _controls_key(self, run_id: str) -> str:
        return f"{self.prefix}:run:{run_id}:controls"

    def _lease_key(self, run_id: str) -> str:
        return f"{self.prefix}:run:{run_id}:lease"

    def _worker_key(self, worker_id: str) -> str:
        return f"{self.prefix}:worker:{worker_id}"

    async def ping(self) -> bool:
        return bool(await self.redis.ping())

    async def close(self) -> None:
        await self.redis.aclose()

    async def _ensure_group(self) -> None:
        if self._group_ready:
            return
        try:
            await self.redis.xgroup_create(self.jobs_key, self.worker_group, id="0-0", mkstream=True)
        except Exception as exc:
            if "BUSYGROUP" not in str(exc):
                raise
        self._group_ready = True

    async def publish_event(self, event: dict[str, Any]) -> None:
        run_id = str(event.get("runId") or "")
        if not run_id:
            return
        await self.redis.xadd(
            self._events_key(run_id),
            {
                "event_id": str(event.get("id") or 0),
                "data": json.dumps(event, ensure_ascii=False, default=str),
            },
            maxlen=20_000,
            approximate=True,
        )

    async def stream_events(self, run_id: str, after_event_id: int = 0) -> AsyncIterator[dict[str, Any]]:
        cursor = "0-0"
        key = self._events_key(run_id)
        while True:
            rows = await self.redis.xread({key: cursor}, count=200, block=10_000)
            if not rows:
                continue
            for _, messages in rows:
                for message_id, fields in messages:
                    cursor = _decode(message_id)
                    raw_event_id = fields.get(b"event_id") or fields.get("event_id") or 0
                    try:
                        event_id = int(_decode(raw_event_id))
                    except ValueError:
                        event_id = 0
                    if event_id <= after_event_id:
                        continue
                    raw = fields.get(b"data") or fields.get("data") or b"{}"
                    try:
                        event = json.loads(_decode(raw))
                    except json.JSONDecodeError:
                        continue
                    if isinstance(event, dict):
                        yield event

    async def enqueue(self, run_id: str, action: str, payload: dict[str, Any] | None = None) -> str:
        message_id = await self.redis.xadd(
            self.jobs_key,
            {
                "run_id": run_id,
                "action": action,
                "payload": json.dumps(payload or {}, ensure_ascii=False, default=str),
                "command_id": uuid.uuid4().hex,
            },
            maxlen=10_000,
            approximate=True,
        )
        return _decode(message_id)

    @staticmethod
    def _job(message_id: Any, fields: dict[Any, Any]) -> ClaimedJob | None:
        run_id = _decode(fields.get(b"run_id") or fields.get("run_id") or "").strip()
        action = _decode(fields.get(b"action") or fields.get("action") or "start").strip()
        if not run_id:
            return None
        raw_payload = fields.get(b"payload") or fields.get("payload") or b"{}"
        try:
            payload = json.loads(_decode(raw_payload))
        except json.JSONDecodeError:
            payload = {}
        return ClaimedJob(_decode(message_id), run_id, action, payload if isinstance(payload, dict) else {})

    async def claim_job(self, worker_id: str, *, block_ms: int = 5000) -> ClaimedJob | None:
        await self._ensure_group()
        try:
            claimed = await self.redis.xautoclaim(
                self.jobs_key,
                self.worker_group,
                worker_id,
                min_idle_time=self.reclaim_idle_ms,
                start_id="0-0",
                count=1,
            )
            messages = claimed[1] if isinstance(claimed, (list, tuple)) and len(claimed) > 1 else []
            if messages:
                return self._job(messages[0][0], messages[0][1])
        except Exception as exc:
            # Older Redis servers may not support XAUTOCLAIM. New jobs remain
            # available; health output exposes the degraded recovery mode.
            if "unknown command" not in str(exc).lower():
                raise
        rows = await self.redis.xreadgroup(
            self.worker_group,
            worker_id,
            {self.jobs_key: ">"},
            count=1,
            block=max(1, int(block_ms)),
        )
        if not rows:
            return None
        _, messages = rows[0]
        return self._job(messages[0][0], messages[0][1]) if messages else None

    async def ack_job(self, message_id: str) -> None:
        await self.redis.xack(self.jobs_key, self.worker_group, message_id)

    async def acquire_lease(self, run_id: str, worker_id: str, ttl_seconds: int) -> RunLease | None:
        lease = RunLease(run_id, worker_id, uuid.uuid4().hex)
        value = json.dumps({"worker_id": worker_id, "token": lease.token}, ensure_ascii=False)
        acquired = await self.redis.set(
            self._lease_key(run_id), value, nx=True, px=max(5, int(ttl_seconds)) * 1000
        )
        return lease if acquired else None

    async def renew_lease(self, lease: RunLease, ttl_seconds: int) -> bool:
        result = await self.redis.eval(
            self._renew_script,
            1,
            self._lease_key(lease.run_id),
            lease.token,
            max(5, int(ttl_seconds)) * 1000,
        )
        return bool(result)

    async def release_lease(self, lease: RunLease) -> bool:
        return bool(await self.redis.eval(
            self._release_script, 1, self._lease_key(lease.run_id), lease.token
        ))

    async def lease_owner(self, run_id: str) -> dict[str, Any] | None:
        raw = await self.redis.get(self._lease_key(run_id))
        if not raw:
            return None
        try:
            value = json.loads(_decode(raw))
        except json.JSONDecodeError:
            return None
        return value if isinstance(value, dict) else None

    async def send_control(self, run_id: str, action: str, payload: dict[str, Any] | None = None) -> str:
        message_id = await self.redis.xadd(
            self._controls_key(run_id),
            {
                "action": action,
                "payload": json.dumps(payload or {}, ensure_ascii=False, default=str),
                "command_id": uuid.uuid4().hex,
            },
            maxlen=1000,
            approximate=True,
        )
        return _decode(message_id)

    async def read_controls(
        self, run_id: str, after_id: str, *, block_ms: int = 1000
    ) -> tuple[str, list[dict[str, Any]]]:
        rows = await self.redis.xread(
            {self._controls_key(run_id): after_id}, count=50, block=max(1, int(block_ms))
        )
        cursor = after_id
        controls: list[dict[str, Any]] = []
        for _, messages in rows or []:
            for message_id, fields in messages:
                cursor = _decode(message_id)
                action = _decode(fields.get(b"action") or fields.get("action") or "")
                raw_payload = fields.get(b"payload") or fields.get("payload") or b"{}"
                try:
                    payload = json.loads(_decode(raw_payload))
                except json.JSONDecodeError:
                    payload = {}
                controls.append({"id": cursor, "action": action, "payload": payload})
        return cursor, controls

    async def heartbeat_worker(self, worker_id: str, ttl_seconds: int = 30) -> None:
        value = json.dumps({"worker_id": worker_id}, ensure_ascii=False)
        await self.redis.set(self._worker_key(worker_id), value, ex=max(10, int(ttl_seconds)))

    async def remove_worker(self, worker_id: str) -> None:
        await self.redis.delete(self._worker_key(worker_id))

    async def active_workers(self) -> list[dict[str, Any]]:
        workers: list[dict[str, Any]] = []
        async for raw_key in self.redis.scan_iter(match=f"{self.prefix}:worker:*", count=100):
            raw = await self.redis.get(raw_key)
            if not raw:
                continue
            try:
                value = json.loads(_decode(raw))
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                workers.append(value)
        return workers


def create_run_coordinator_from_env() -> RedisRunCoordinator | None:
    url = os.getenv("REDIS_URL", "").strip()
    if not url:
        return None
    try:
        reclaim_idle_ms = int(os.getenv("REDIS_RECLAIM_IDLE_MS", "30000"))
    except ValueError:
        reclaim_idle_ms = 30_000
    return RedisRunCoordinator(
        url,
        prefix=os.getenv("REDIS_PREFIX", "agent-team"),
        worker_group=os.getenv("REDIS_WORKER_GROUP", "agent-team-workers"),
        reclaim_idle_ms=reclaim_idle_ms,
    )
