"""Small SQLite repository for durable run and step history."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from typing import Any, Callable, TypeVar, cast


T = TypeVar("T")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sqlite_retry(function: Callable[..., T]) -> Callable[..., T]:
    """Serialize repository access and retry short-lived SQLite write locks."""
    @wraps(function)
    def wrapped(self: "SQLiteRepository", *args: Any, **kwargs: Any) -> T:
        for attempt in range(8):
            try:
                with self._lock:
                    return function(self, *args, **kwargs)
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower() or attempt == 7:
                    raise
                time.sleep(0.05 * (attempt + 1))
        raise RuntimeError("SQLite operation did not complete")

    return cast(Callable[..., T], wrapped)


class SQLiteRepository:
    def __init__(self, database_path: str | Path = "data/agent_team.db") -> None:
        self.database_path = str(database_path)
        self._lock = threading.RLock()
        if self.database_path != ":memory:":
            Path(self.database_path).parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.database_path, timeout=30.0, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA busy_timeout = 30000")
        if self.database_path != ":memory:":
            self._connection.execute("PRAGMA journal_mode = WAL")
            self._connection.execute("PRAGMA synchronous = NORMAL")
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS workflow_runs (
                id TEXT PRIMARY KEY, workflow_id TEXT NOT NULL, status TEXT NOT NULL,
                started_at TEXT, finished_at TEXT, duration_ms INTEGER DEFAULT 0,
                error_message TEXT, final_report TEXT, inputs_json TEXT,
                state_json TEXT, workflow_snapshot_json TEXT,
                heartbeat_at TEXT, recovery_count INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS step_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, step_id TEXT NOT NULL,
                agent_id TEXT, status TEXT NOT NULL, output TEXT, started_at TEXT, finished_at TEXT,
                duration_ms INTEGER DEFAULT 0, retry_count INTEGER DEFAULT 0,
                error_message TEXT, input_tokens INTEGER DEFAULT 0, output_tokens INTEGER DEFAULT 0,
                finish_reason TEXT, message_content TEXT, reasoning_content TEXT,
                usage_json TEXT, provider_attempts_json TEXT,
                UNIQUE(run_id, step_id)
            );
            CREATE TABLE IF NOT EXISTS approval_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, step_id TEXT NOT NULL,
                status TEXT NOT NULL, decision TEXT, created_at TEXT, resolved_at TEXT,
                UNIQUE(run_id, step_id)
            );
            CREATE TABLE IF NOT EXISTS run_artifacts (
                id TEXT PRIMARY KEY, run_id TEXT NOT NULL, name TEXT NOT NULL,
                mime_type TEXT NOT NULL, relative_path TEXT NOT NULL,
                size_bytes INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL,
                sha256 TEXT, owner_step TEXT,
                UNIQUE(run_id, name)
            );
            CREATE TABLE IF NOT EXISTS run_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL, event_type TEXT NOT NULL,
                timestamp TEXT NOT NULL, payload_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS artifact_versions (
                id TEXT PRIMARY KEY, run_id TEXT NOT NULL, name TEXT NOT NULL,
                version INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL,
                owner_step TEXT, parent_version INTEGER, sha256 TEXT,
                created_at TEXT NOT NULL, metadata_json TEXT,
                UNIQUE(run_id, name, version)
            );
            CREATE TABLE IF NOT EXISTS collaboration_messages (
                id TEXT PRIMARY KEY, run_id TEXT NOT NULL, conversation_id TEXT NOT NULL,
                sender TEXT NOT NULL, recipient TEXT NOT NULL, act TEXT NOT NULL,
                round_number INTEGER NOT NULL, payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS run_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL, step_id TEXT NOT NULL,
                attempt_index INTEGER NOT NULL,
                provider_json TEXT NOT NULL,
                input_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                finish_reason TEXT,
                created_at TEXT NOT NULL,
                UNIQUE(run_id, step_id, attempt_index)
            );
            CREATE TABLE IF NOT EXISTS run_worker_leases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL, worker_id TEXT NOT NULL,
                lease_token TEXT NOT NULL, acquired_at TEXT NOT NULL,
                released_at TEXT, release_reason TEXT,
                UNIQUE(run_id, lease_token)
            );
            CREATE INDEX IF NOT EXISTS idx_collaboration_run_conversation
                ON collaboration_messages(run_id, conversation_id, round_number, created_at);
            CREATE INDEX IF NOT EXISTS idx_run_events_run_id_id ON run_events(run_id, id);
            CREATE INDEX IF NOT EXISTS idx_run_attempts_run_step ON run_attempts(run_id, step_id, attempt_index);
            CREATE INDEX IF NOT EXISTS idx_run_worker_leases_run ON run_worker_leases(run_id, id);
            """
        )
        self._ensure_run_columns()
        self._ensure_step_columns()
        self._ensure_artifact_columns()
        self._connection.commit()

    def _ensure_run_columns(self) -> None:
        existing = {str(row[1]) for row in self._connection.execute("PRAGMA table_info(workflow_runs)").fetchall()}
        columns = {
            "state_json": "TEXT",
            "workflow_snapshot_json": "TEXT",
            "heartbeat_at": "TEXT",
            "recovery_count": "INTEGER NOT NULL DEFAULT 0",
            "execution_status": "TEXT",
            "delivery_status": "TEXT",
            "clarification_json": "TEXT",
            "blueprint_json": "TEXT",
            "evidence_json": "TEXT",
            "failure_facts_json": "TEXT",
            "decision_log_json": "TEXT",
            "assumption_log_json": "TEXT",
        }
        for name, sql_type in columns.items():
            if name not in existing:
                self._connection.execute(f"ALTER TABLE workflow_runs ADD COLUMN {name} {sql_type}")

    def _ensure_step_columns(self) -> None:
        existing = {str(row[1]) for row in self._connection.execute("PRAGMA table_info(step_runs)").fetchall()}
        columns = {
            "finish_reason": "TEXT",
            "message_content": "TEXT",
            "reasoning_content": "TEXT",
            "usage_json": "TEXT",
            "provider_attempts_json": "TEXT",
        }
        for name, sql_type in columns.items():
            if name not in existing:
                self._connection.execute(f"ALTER TABLE step_runs ADD COLUMN {name} {sql_type}")

    def _ensure_artifact_columns(self) -> None:
        existing = {str(row[1]) for row in self._connection.execute("PRAGMA table_info(run_artifacts)").fetchall()}
        columns = {"sha256": "TEXT", "owner_step": "TEXT"}
        for name, sql_type in columns.items():
            if name not in existing:
                self._connection.execute(f"ALTER TABLE run_artifacts ADD COLUMN {name} {sql_type}")

    @_sqlite_retry
    def create_run(self, run_id: str, workflow_id: str, inputs: dict[str, Any], status: str, started_at: str) -> None:
        self._connection.execute(
            "INSERT INTO workflow_runs (id, workflow_id, status, inputs_json, started_at, heartbeat_at, execution_status, delivery_status) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (run_id, workflow_id, status, json.dumps(inputs, ensure_ascii=False), started_at, started_at, status, "NOT_EVALUATED"),
        )
        self._connection.commit()

    @_sqlite_retry
    def update_run(self, run_id: str, **fields: Any) -> None:
        if not fields:
            return
        allowed = {
            "status", "finished_at", "duration_ms", "error_message", "final_report",
            "heartbeat_at", "recovery_count", "workflow_snapshot_json", "state_json",
            "execution_status", "delivery_status", "clarification_json", "blueprint_json",
            "evidence_json", "failure_facts_json", "decision_log_json", "assumption_log_json",
        }
        fields = {key: value for key, value in fields.items() if key in allowed}
        if not fields:
            return
        assignment = ", ".join(f"{key} = ?" for key in fields)
        self._connection.execute(
            f"UPDATE workflow_runs SET {assignment} WHERE id = ?", (*fields.values(), run_id)
        )
        self._connection.commit()

    @_sqlite_retry
    def save_run_snapshot(
        self,
        run_id: str,
        snapshot: dict[str, Any],
        *,
        heartbeat_at: str | None = None,
    ) -> None:
        """Persist the resumable domain state beside LangGraph's checkpoint.

        The LangGraph checkpoint is the execution engine's state.  This copy is
        intentionally small in scope and gives the platform a provider-neutral
        recovery record when a process is restarted or a checkpoint is missing.
        """
        self._connection.execute(
            "UPDATE workflow_runs SET state_json = ?, heartbeat_at = COALESCE(?, heartbeat_at) WHERE id = ?",
            (json.dumps(snapshot, ensure_ascii=False, default=str), heartbeat_at or utc_now(), run_id),
        )
        self._connection.commit()

    @_sqlite_retry
    def save_workflow_snapshot(self, run_id: str, snapshot: dict[str, Any]) -> None:
        self._connection.execute(
            "UPDATE workflow_runs SET workflow_snapshot_json = ? WHERE id = ?",
            (json.dumps(snapshot, ensure_ascii=False, default=str), run_id),
        )
        self._connection.commit()

    @_sqlite_retry
    def mark_recovery(self, run_id: str, *, status: str | None = None) -> int:
        assignment = "recovery_count = recovery_count + 1, heartbeat_at = ?"
        values: list[Any] = [utc_now()]
        if status is not None:
            assignment += ", status = ?"
            values.append(status)
        values.append(run_id)
        self._connection.execute(f"UPDATE workflow_runs SET {assignment} WHERE id = ?", values)
        self._connection.commit()
        row = self._connection.execute("SELECT recovery_count FROM workflow_runs WHERE id = ?", (run_id,)).fetchone()
        return int(row["recovery_count"] or 0) if row else 0

    @_sqlite_retry
    def append_event(self, run_id: str, event_type: str, timestamp: str, payload: dict[str, Any]) -> int:
        cursor = self._connection.execute(
            "INSERT INTO run_events (run_id, event_type, timestamp, payload_json) VALUES (?, ?, ?, ?)",
            (run_id, event_type, timestamp, json.dumps(payload, ensure_ascii=False, default=str)),
        )
        self._connection.commit()
        return int(cursor.lastrowid)

    @_sqlite_retry
    def list_events(self, run_id: str, after_id: int = 0) -> list[dict[str, Any]]:
        rows = self._connection.execute(
            "SELECT id, run_id, event_type, timestamp, payload_json FROM run_events WHERE run_id = ? AND id > ? ORDER BY id",
            (run_id, max(0, int(after_id))),
        ).fetchall()
        return [
            {
                "id": int(row["id"]),
                "type": str(row["event_type"]),
                "runId": str(row["run_id"]),
                "timestamp": str(row["timestamp"]),
                "payload": _decode_json(row["payload_json"], {}),
            }
            for row in rows
        ]

    @_sqlite_retry
    def append_collaboration_message(
        self,
        *,
        message_id: str,
        run_id: str,
        conversation_id: str,
        sender: str,
        recipient: str,
        act: str,
        round_number: int,
        payload: dict[str, Any],
    ) -> None:
        """Idempotent, run-scoped mailbox record; never stores a full prompt."""
        self._connection.execute(
            """INSERT OR IGNORE INTO collaboration_messages
                (id, run_id, conversation_id, sender, recipient, act,
                 round_number, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (message_id, run_id, conversation_id, sender, recipient, act,
             round_number, json.dumps(payload, ensure_ascii=False, default=str), utc_now()),
        )
        self._connection.commit()

    @_sqlite_retry
    def record_provider_attempts(self, run_id: str, step_id: str, attempts: list[dict[str, Any]]) -> None:
        for index, attempt in enumerate(attempts, start=1):
            usage = attempt.get("usage") if isinstance(attempt.get("usage"), dict) else {}
            input_tokens = attempt.get("input_tokens", usage.get("prompt_tokens", usage.get("input_tokens", 0)))
            output_tokens = attempt.get("output_tokens", usage.get("completion_tokens", usage.get("output_tokens", 0)))
            self._connection.execute(
                """
                INSERT INTO run_attempts
                    (run_id, step_id, attempt_index, provider_json, input_tokens, output_tokens, finish_reason, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id, step_id, attempt_index) DO UPDATE SET
                    provider_json = excluded.provider_json,
                    input_tokens = excluded.input_tokens,
                    output_tokens = excluded.output_tokens,
                    finish_reason = excluded.finish_reason
                """,
                (
                    run_id, step_id, index,
                    json.dumps(attempt, ensure_ascii=False, default=str),
                    int(input_tokens or 0), int(output_tokens or 0),
                    attempt.get("finish_reason"), utc_now(),
                ),
            )
        self._connection.commit()

    @_sqlite_retry
    def list_provider_attempts(self, run_id: str) -> list[dict[str, Any]]:
        rows = self._connection.execute(
            "SELECT * FROM run_attempts WHERE run_id = ? ORDER BY step_id, attempt_index",
            (run_id,),
        ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["provider"] = _decode_json(item.pop("provider_json", None), {})
            result.append(item)
        return result

    @_sqlite_retry
    def record_worker_lease(self, run_id: str, worker_id: str, lease_token: str) -> None:
        self._connection.execute(
            "INSERT OR IGNORE INTO run_worker_leases (run_id, worker_id, lease_token, acquired_at) VALUES (?, ?, ?, ?)",
            (run_id, worker_id, lease_token, utc_now()),
        )
        self._connection.commit()

    @_sqlite_retry
    def release_worker_lease(self, run_id: str, lease_token: str, reason: str) -> None:
        self._connection.execute(
            "UPDATE run_worker_leases SET released_at = ?, release_reason = ? WHERE run_id = ? AND lease_token = ?",
            (utc_now(), reason, run_id, lease_token),
        )
        self._connection.commit()

    @_sqlite_retry
    def list_worker_leases(self, run_id: str) -> list[dict[str, Any]]:
        return [dict(row) for row in self._connection.execute(
            "SELECT * FROM run_worker_leases WHERE run_id = ? ORDER BY id", (run_id,)
        ).fetchall()]

    @_sqlite_retry
    def list_run_summaries(self, *, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        rows = self._connection.execute(
            """
            SELECT id, workflow_id, status, execution_status, delivery_status,
                   started_at, finished_at, duration_ms, heartbeat_at,
                   recovery_count, error_message, inputs_json
            FROM workflow_runs
            ORDER BY rowid DESC LIMIT ? OFFSET ?
            """,
            (max(1, min(200, int(limit))), max(0, int(offset))),
        ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            inputs = _decode_json(item.pop("inputs_json", None), {})
            item["requirement"] = str(inputs.get("requirement") or "")[:500]
            result.append(item)
        return result

    @_sqlite_retry
    def list_collaboration_messages(
        self, run_id: str, conversation_id: str | None = None,
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM collaboration_messages WHERE run_id = ?"
        args: tuple[str, ...] = (run_id,)
        if conversation_id is not None:
            query += " AND conversation_id = ?"
            args = (run_id, conversation_id)
        query += " ORDER BY round_number, created_at, id"
        rows = self._connection.execute(query, args).fetchall()
        return [
            {**{key: value for key, value in dict(row).items() if key != "payload_json"},
             "payload": _decode_json(row["payload_json"], {})}
            for row in rows
        ]

    @_sqlite_retry
    def update_run_inputs(self, run_id: str, updates: dict[str, Any]) -> None:
        """Merge non-secret, run-scoped inputs for durable audit/recovery."""
        row = self._connection.execute(
            "SELECT inputs_json FROM workflow_runs WHERE id = ?", (run_id,)
        ).fetchone()
        if not row:
            return
        current = _decode_json(row["inputs_json"], {})
        if not isinstance(current, dict):
            current = {}
        current.update(updates)
        self._connection.execute(
            "UPDATE workflow_runs SET inputs_json = ? WHERE id = ?",
            (json.dumps(current, ensure_ascii=False), run_id),
        )
        self._connection.commit()

    @_sqlite_retry
    def upsert_step(self, run_id: str, step_id: str, **fields: Any) -> None:
        row = self._connection.execute(
            "SELECT id FROM step_runs WHERE run_id = ? AND step_id = ?", (run_id, step_id)
        ).fetchone()
        if row:
            allowed = {
                "agent_id", "status", "output", "started_at", "finished_at", "duration_ms", "retry_count",
                "error_message", "input_tokens", "output_tokens", "finish_reason", "message_content",
                "reasoning_content", "usage_json", "provider_attempts_json",
            }
            fields = {key: value for key, value in fields.items() if key in allowed}
            assignment = ", ".join(f"{key} = ?" for key in fields)
            if assignment:
                self._connection.execute(f"UPDATE step_runs SET {assignment} WHERE id = ?", (*fields.values(), row["id"]))
        else:
            columns = ["run_id", "step_id", *fields.keys()]
            values = [run_id, step_id, *fields.values()]
            placeholders = ", ".join("?" for _ in columns)
            self._connection.execute(f"INSERT INTO step_runs ({', '.join(columns)}) VALUES ({placeholders})", values)
        self._connection.commit()

    @_sqlite_retry
    def create_approval(self, run_id: str, step_id: str, created_at: str) -> None:
        self._connection.execute(
            """
            INSERT INTO approval_requests (run_id, step_id, status, created_at)
            VALUES (?, ?, 'WAITING', ?)
            ON CONFLICT(run_id, step_id) DO UPDATE SET
                status = 'WAITING',
                decision = NULL,
                created_at = excluded.created_at,
                resolved_at = NULL
            """,
            (run_id, step_id, created_at),
        )
        self._connection.commit()

    @_sqlite_retry
    def resolve_approval(self, run_id: str, step_id: str, decision: str, resolved_at: str) -> None:
        self._connection.execute(
            "UPDATE approval_requests SET status = 'RESOLVED', decision = ?, resolved_at = ? WHERE run_id = ? AND step_id = ?",
            (decision, resolved_at, run_id, step_id),
        )
        self._connection.commit()

    @_sqlite_retry
    def get_run(self, run_id: str) -> dict[str, Any] | None:
        row = self._connection.execute("SELECT * FROM workflow_runs WHERE id = ?", (run_id,)).fetchone()
        if not row:
            return None
        result = dict(row)
        result["inputs"] = json.loads(result.pop("inputs_json") or "{}")
        result["state"] = _decode_json(result.pop("state_json", None), {})
        result["workflow_snapshot"] = _decode_json(result.pop("workflow_snapshot_json", None), {})
        for raw_key, public_key in (
            ("clarification_json", "clarification"),
            ("blueprint_json", "blueprint"),
            ("evidence_json", "evidence"),
            ("failure_facts_json", "failure_facts"),
            ("decision_log_json", "decision_log"),
            ("assumption_log_json", "assumption_log"),
        ):
            result[public_key] = _decode_json(result.pop(raw_key, None), {} if public_key not in {"evidence", "failure_facts", "decision_log", "assumption_log"} else [])
        result["execution_status"] = result.get("execution_status") or result.get("status")
        result["delivery_status"] = result.get("delivery_status") or "NOT_EVALUATED"
        result["steps"] = []
        for step in self._connection.execute("SELECT * FROM step_runs WHERE run_id = ? ORDER BY id", (run_id,)).fetchall():
            item = dict(step)
            usage = _decode_json(item.pop("usage_json", None), {})
            attempts = _decode_json(item.pop("provider_attempts_json", None), [])
            persisted_provider = {
                "finish_reason": item.get("finish_reason"),
                "message_content": item.get("message_content"),
                "reasoning_content": item.get("reasoning_content"),
                "usage": usage,
            }
            item["provider_attempts"] = attempts if isinstance(attempts, list) else []
            if item["provider_attempts"] and isinstance(item["provider_attempts"][-1], dict):
                persisted_provider.update(item["provider_attempts"][-1])
            item["provider"] = persisted_provider
            result["steps"].append(item)
        approval_rows = self._connection.execute(
            "SELECT created_at, resolved_at FROM approval_requests WHERE run_id = ? ORDER BY id",
            (run_id,),
        ).fetchall()
        metric_events = self._connection.execute(
            """
            SELECT event_type, timestamp, payload_json
            FROM run_events
            WHERE run_id = ?
            ORDER BY id
            """,
            (run_id,),
        ).fetchall()
        result.update(_run_metrics(result, approval_rows, metric_events))
        result["artifacts"] = self.list_artifacts(run_id)
        result["collaboration"] = self.list_collaboration_messages(run_id)
        result["attempts"] = self.list_provider_attempts(run_id)
        result["worker_leases"] = self.list_worker_leases(run_id)
        return result

    @_sqlite_retry
    def upsert_artifact(
        self,
        artifact_id: str,
        run_id: str,
        name: str,
        mime_type: str,
        relative_path: str,
        size_bytes: int,
        created_at: str,
        sha256: str | None = None,
        owner_step: str | None = None,
    ) -> None:
        self._connection.execute(
            """
            INSERT INTO run_artifacts
                (id, run_id, name, mime_type, relative_path, size_bytes, created_at, sha256, owner_step)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id, name) DO UPDATE SET
                id = excluded.id,
                mime_type = excluded.mime_type,
                relative_path = excluded.relative_path,
                size_bytes = excluded.size_bytes,
                created_at = excluded.created_at,
                sha256 = excluded.sha256,
                owner_step = excluded.owner_step
            """,
            (artifact_id, run_id, name, mime_type, relative_path, size_bytes, created_at, sha256, owner_step),
        )
        self._connection.commit()

    @_sqlite_retry
    def list_artifacts(self, run_id: str) -> list[dict[str, Any]]:
        rows = self._connection.execute(
            "SELECT * FROM run_artifacts WHERE run_id = ? ORDER BY created_at, name",
            (run_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    @_sqlite_retry
    def get_artifact(self, run_id: str, artifact_id: str) -> dict[str, Any] | None:
        row = self._connection.execute(
            "SELECT * FROM run_artifacts WHERE run_id = ? AND id = ?",
            (run_id, artifact_id),
        ).fetchone()
        return dict(row) if row else None

    @_sqlite_retry
    def record_artifact_version(
        self,
        *,
        run_id: str,
        name: str,
        status: str,
        owner_step: str | None,
        sha256: str | None,
        metadata: dict[str, Any] | None = None,
        parent_version: int | None = None,
    ) -> dict[str, Any]:
        row = self._connection.execute(
            "SELECT COALESCE(MAX(version), 0) AS version FROM artifact_versions WHERE run_id = ? AND name = ?",
            (run_id, name),
        ).fetchone()
        version = int(row["version"] or 0) + 1
        version_id = f"{run_id}_{name.replace('/', '_').replace('.', '_')}_v{version}"
        created_at = utc_now()
        self._connection.execute(
            "INSERT INTO artifact_versions (id, run_id, name, version, status, owner_step, parent_version, sha256, created_at, metadata_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (version_id, run_id, name, version, status, owner_step, parent_version, sha256, created_at, json.dumps(metadata or {}, ensure_ascii=False, default=str)),
        )
        self._connection.commit()
        return {"id": version_id, "run_id": run_id, "name": name, "version": version, "status": status, "owner_step": owner_step, "parent_version": parent_version, "sha256": sha256, "created_at": created_at, "metadata": metadata or {}}

    @_sqlite_retry
    def list_artifact_versions(self, run_id: str) -> list[dict[str, Any]]:
        rows = self._connection.execute("SELECT * FROM artifact_versions WHERE run_id = ? ORDER BY name, version", (run_id,)).fetchall()
        return [{**dict(row), "metadata": _decode_json(row["metadata_json"], {})} for row in rows]

    @_sqlite_retry
    def list_runs(self, status: str | None = None) -> list[dict[str, Any]]:
        if status is None:
            rows = self._connection.execute("SELECT id FROM workflow_runs ORDER BY rowid").fetchall()
        else:
            rows = self._connection.execute("SELECT id FROM workflow_runs WHERE status = ? ORDER BY rowid", (status,)).fetchall()
        return [run for row in rows if (run := self.get_run(row["id"])) is not None]

    @_sqlite_retry
    def list_recoverable_runs(self) -> list[dict[str, Any]]:
        """Return non-terminal Runs that must be reconciled on startup."""
        rows = self._connection.execute(
            "SELECT id FROM workflow_runs WHERE status IN (?, ?, ?, ?) ORDER BY rowid",
            ("PENDING", "RUNNING", "WAITING_APPROVAL", "WAITING_CLARIFICATION"),
        ).fetchall()
        return [run for row in rows if (run := self.get_run(row["id"])) is not None]


def _decode_json(value: Any, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _parse_timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _non_negative_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _run_metrics(
    run: dict[str, Any],
    approval_rows: list[sqlite3.Row],
    metric_events: list[sqlite3.Row] | None = None,
) -> dict[str, Any]:
    """Return live duration and token totals without changing the DB schema."""
    now = datetime.now(timezone.utc)
    started_at = _parse_timestamp(run.get("started_at"))
    finished_at = _parse_timestamp(run.get("finished_at")) or now
    wall_duration_ms = (
        max(0, int((finished_at - started_at).total_seconds() * 1000))
        if started_at
        else _non_negative_int(run.get("duration_ms"))
    )

    approval_duration_ms = 0
    for row in approval_rows:
        approval_started = _parse_timestamp(row["created_at"])
        approval_finished = _parse_timestamp(row["resolved_at"]) or now
        if approval_started:
            approval_duration_ms += max(
                0,
                int((approval_finished - approval_started).total_seconds() * 1000),
            )

    cumulative_tokens: dict[str, tuple[int, int]] = {}
    decoded_events: list[tuple[str, dict[str, Any]]] = []
    for row in metric_events or []:
        event_type = str(row["event_type"] or "")
        payload = _decode_json(row["payload_json"], {})
        if not isinstance(payload, dict):
            continue
        decoded_events.append((event_type, payload))
        if event_type not in {"step.completed", "step.failed"}:
            continue
        step_id = str(payload.get("stepId") or "").strip()
        tokens = payload.get("tokens")
        if not step_id or not isinstance(tokens, dict):
            continue
        previous_input, previous_output = cumulative_tokens.get(step_id, (0, 0))
        cumulative_tokens[step_id] = (
            previous_input + _non_negative_int(tokens.get("input")),
            previous_output + _non_negative_int(tokens.get("output")),
        )

    by_agent: list[dict[str, Any]] = []
    step_durations: dict[str, int] = {}
    total_input_tokens = 0
    total_output_tokens = 0
    for step in run.get("steps", []):
        if not isinstance(step, dict) or not str(step.get("agent_id") or "").strip():
            continue
        step_id = str(step.get("step_id") or "")
        input_tokens, output_tokens = cumulative_tokens.get(
            step_id,
            (
                _non_negative_int(step.get("input_tokens")),
                _non_negative_int(step.get("output_tokens")),
            ),
        )
        total_input_tokens += input_tokens
        total_output_tokens += output_tokens
        step_durations[step_id] = _non_negative_int(step.get("duration_ms"))
        by_agent.append(
            {
                "step_id": step_id,
                "agent_id": str(step.get("agent_id") or ""),
                "status": str(step.get("status") or ""),
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": input_tokens + output_tokens,
                "retry_count": _non_negative_int(step.get("retry_count")),
            }
        )

    correction_events = {
        "step.retrying", "step.continuing", "step.repairing", "step.artifact_repairing",
        "step.artifact_continuing", "step.validation_repairing", "step.validation_owner_reexecuting",
        "step.repaired", "step.artifact_plan_fallback", "repair.round_started", "workflow.recovered",
        "architecture.repair_started", "delivery.repair_started", "delivery.archive_rebuild_started",
    }
    repair_rounds = {
        (str(payload.get("stepId") or "tester"), _non_negative_int(payload.get("repairAttempt")))
        for event_type, payload in decoded_events
        if event_type in {"repair.round_started", "architecture.repair_started", "delivery.repair_started"}
        and _non_negative_int(payload.get("repairAttempt")) > 0
    }
    repair_completions = [
        payload for event_type, payload in decoded_events
        if event_type in {"repair.completed", "delivery.repair_completed", "delivery.archive_rebuild_completed"}
    ]
    architecture_attempted = any(event_type == "architecture.repair_started" for event_type, _ in decoded_events)
    if architecture_attempted:
        architecture_passed = any(
            event_type == "architecture.target_gate_completed" and bool(payload.get("passed"))
            for event_type, payload in decoded_events
        )
        repair_completions.append({"passed": architecture_passed})
    successful_repairs = sum(1 for payload in repair_completions if bool(payload.get("passed")))
    coordinator_count = len(repair_completions)
    first_pass = str(run.get("status") or "") == "SUCCESS" and not any(
        event_type in correction_events for event_type, _ in decoded_events
    )

    stage_map = {
        "token_estimator": "analysis", "requirement": "analysis", "architecture": "analysis",
        "database": "implementation", "backend": "implementation", "frontend": "implementation",
        "tester": "validation", "reviewer": "review",
    }
    stages: dict[str, dict[str, Any]] = {}
    for row in by_agent:
        stage = stage_map.get(str(row["step_id"]), str(row["step_id"]))
        current = stages.setdefault(stage, {"stage": stage, "duration_ms": 0, "input_tokens": 0, "output_tokens": 0, "total_tokens": 0})
        current["duration_ms"] += step_durations.get(str(row["step_id"]), 0)
        current["input_tokens"] += _non_negative_int(row.get("input_tokens"))
        current["output_tokens"] += _non_negative_int(row.get("output_tokens"))
        current["total_tokens"] = current["input_tokens"] + current["output_tokens"]
    for event_type, payload in decoded_events:
        if event_type != "step.validation_stage_completed":
            continue
        name = f"validation_{str(payload.get('stage') or 'unknown')}"
        current = stages.setdefault(name, {"stage": name, "duration_ms": 0, "input_tokens": 0, "output_tokens": 0, "total_tokens": 0})
        current["duration_ms"] += _non_negative_int(payload.get("durationMs") or payload.get("duration_ms"))

    trace_status = {
        "architecture.contract_failed": "合同预检失败",
        "architecture.repair_started": "Architecture 定点修复中",
        "architecture.repair_rejected": "修复提案未通过验收",
        "architecture.target_gate_completed": "合同目标 Gate 通过",
        "architecture.repair_circuit_open": "无进展熔断",
        "repair.routed": "已定位",
        "repair.round_started": "责任 Agent 整改中",
        "repair.target_gate_completed": "目标 Gate 已验证",
        "repair.full_regression_completed": "完整回归已验证",
        "repair.completed": "自动修复已结束",
        "repair.circuit_open": "无进展熔断",
        "delivery.repair_started": "交付门禁整改中",
        "delivery.repair_completed": "交付门禁已验证",
        "delivery.archive_rebuild_started": "交付压缩包重建中",
        "delivery.archive_rebuild_completed": "交付压缩包已验证",
    }
    repair_trace: dict[str, dict[str, Any]] = {}
    for event_type, payload in decoded_events:
        if event_type in {"step.validation_succeeded", "step.validation_failed"}:
            architecture_rows = [row for row in repair_trace.values() if row["step_id"] == "architecture" and row["attempt"] > 0]
            if architecture_rows:
                latest = max(architecture_rows, key=lambda row: row["attempt"])
                latest["status"] = "完整回归通过" if event_type.endswith("succeeded") else "完整回归失败"
                latest["result"] = str((payload.get("result") or {}).get("summary") or "")
            continue
        if event_type not in trace_status:
            continue
        default_step = "architecture" if event_type.startswith("architecture.") else "platform" if event_type.startswith("delivery.archive_") else "tester"
        step_id = str(payload.get("stepId") or default_step)
        attempt = _non_negative_int(payload.get("repairAttempt") or payload.get("attempt") or payload.get("attempts"))
        if event_type.startswith("delivery.archive_"):
            attempt = 1
        key = f"{step_id}:{attempt}"
        if event_type == "architecture.target_gate_completed" and attempt == 0 and key not in repair_trace:
            continue
        row = repair_trace.setdefault(key, {"key": key, "step_id": step_id, "attempt": attempt,
                                           "location": step_id, "owners": [], "status": "", "result": "", "files": []})
        plan = payload.get("plan") if isinstance(payload.get("plan"), dict) else {}
        fact = payload.get("failureFact") if isinstance(payload.get("failureFact"), dict) else {}
        checks = plan.get("check_ids") or plan.get("checkIds") or []
        if isinstance(checks, list) and checks:
            row["location"] = "、".join(map(str, checks))
        elif fact.get("code"):
            row["location"] = str(fact["code"])
        owners = payload.get("owners") or plan.get("owners") or ([fact["owner"]] if fact.get("owner") else [])
        if isinstance(owners, list) and owners:
            row["owners"] = [str(owner) for owner in owners]
        files = payload.get("files")
        if isinstance(files, list) and files:
            row["files"] = [str(name) for name in files]
        row["status"] = trace_status[event_type]
        if event_type in {"architecture.target_gate_completed", "repair.target_gate_completed", "repair.full_regression_completed", "repair.completed", "delivery.repair_completed", "delivery.archive_rebuild_completed"}:
            row["status"] = ("目标 Gate 通过" if event_type.endswith("target_gate_completed") else
                             "完整回归通过" if event_type == "repair.full_regression_completed" else
                             "交付门禁通过" if event_type == "delivery.repair_completed" else
                             "压缩包重建通过" if event_type == "delivery.archive_rebuild_completed" else
                             "自动修复完成" if event_type == "repair.completed" else row["status"]) if payload.get("passed") else row["status"]
        result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
        missing = payload.get("missing") if isinstance(payload.get("missing"), list) else []
        row["result"] = str(result.get("summary") or payload.get("reason") or fact.get("summary") or (f"仍缺少：{'、'.join(map(str, missing))}" if missing else "") or row["result"])

    return {
        "active_duration_ms": max(0, wall_duration_ms - approval_duration_ms),
        "approval_duration_ms": approval_duration_ms,
        "token_usage": {
            "input_tokens": total_input_tokens,
            "output_tokens": total_output_tokens,
            "total_tokens": total_input_tokens + total_output_tokens,
            "by_agent": by_agent,
        },
        "repair_metrics": {
            "first_pass": first_pass,
            "repair_rounds": len(repair_rounds),
            "successful_repairs": successful_repairs,
            "automatic_repair_rate": (successful_repairs / coordinator_count) if coordinator_count else 0.0,
            "average_repair_rounds": (len(repair_rounds) / coordinator_count) if coordinator_count else 0.0,
            "circuit_breaks": sum(1 for event_type, _ in decoded_events if event_type in {"repair.circuit_open", "architecture.repair_circuit_open"}),
        },
        "stage_metrics": list(stages.values()),
        "repair_trace": list(repair_trace.values()),
    }
