"""Durable task records and an append-only event log (same SQLite file as run history)."""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    kind TEXT NOT NULL, mode TEXT NOT NULL, repo TEXT NOT NULL, issue TEXT NOT NULL,
    status TEXT NOT NULL, outcome TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '',
    approval TEXT NOT NULL DEFAULT 'none',
    prompt_tokens INTEGER NOT NULL DEFAULT 0, completion_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0, tool_calls INTEGER NOT NULL DEFAULT 0,
    llm_calls INTEGER NOT NULL DEFAULT 0, retries INTEGER NOT NULL DEFAULT 0,
    elapsed_s REAL NOT NULL DEFAULT 0, limits_json TEXT NOT NULL DEFAULT '{}',
    result_json TEXT NOT NULL DEFAULT '{}', branch TEXT NOT NULL DEFAULT '',
    pr_url TEXT NOT NULL DEFAULT '', pr_number INTEGER NOT NULL DEFAULT 0,
    ci_rounds INTEGER NOT NULL DEFAULT 0, cancel_requested INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, ts TEXT NOT NULL,
    kind TEXT NOT NULL, message TEXT NOT NULL, data_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_events_task ON events(task_id, id);
"""

TERMINAL = {"completed", "failed", "cancelled", "budget_exceeded", "pr_opened", "rejected"}


class TaskRecord(BaseModel):
    id: str
    created_at: str
    updated_at: str
    kind: str
    mode: str
    repo: str
    issue: str
    status: str
    outcome: str = ""
    error: str = ""
    approval: str = "none"
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    tool_calls: int = 0
    llm_calls: int = 0
    retries: int = 0
    elapsed_s: float = 0.0
    limits_json: str = "{}"
    result_json: str = "{}"
    branch: str = ""
    pr_url: str = ""
    pr_number: int = 0
    ci_rounds: int = 0
    cancel_requested: int = 0


class EventRecord(BaseModel):
    id: int
    task_id: str
    ts: str
    kind: str
    message: str
    data: dict[str, Any]


def _now() -> str:
    return datetime.now(UTC).isoformat()


class TaskStore:
    def __init__(self, path: str | Path) -> None:
        self._path = str(path)
        if self._path != ":memory:":
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        with closing(self._conn()) as c, c:
            c.executescript(SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self._path, timeout=30)
        c.row_factory = sqlite3.Row
        return c

    def create(self, *, kind: str, mode: str, repo: str, issue: str,
               limits_json: str = "{}", task_id: str | None = None) -> str:  # fmt: skip
        tid = task_id or uuid.uuid4().hex[:12]
        now = _now()
        with closing(self._conn()) as c, c:
            c.execute(
                "INSERT INTO tasks (id, created_at, updated_at, kind, mode, repo, issue, status,"
                " limits_json) VALUES (?,?,?,?,?,?,?,?,?)",
                (tid, now, now, kind, mode, repo, issue, "queued", limits_json),
            )
        return tid

    def update(self, task_id: str, **fields: Any) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k} = ?" for k in fields)
        with closing(self._conn()) as c, c:
            c.execute(
                f"UPDATE tasks SET {cols}, updated_at = ? WHERE id = ?",  # noqa: S608 (keys are ours)
                (*fields.values(), _now(), task_id),
            )

    def get(self, task_id: str) -> TaskRecord | None:
        with closing(self._conn()) as c:
            row = c.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return TaskRecord(**dict(row)) if row else None

    def recent(self, limit: int = 50) -> list[TaskRecord]:
        with closing(self._conn()) as c:
            rows = c.execute(
                "SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [TaskRecord(**dict(r)) for r in rows]

    def add_event(self, task_id: str, kind: str, message: str, data: dict[str, Any]) -> None:
        with closing(self._conn()) as c, c:
            c.execute(
                "INSERT INTO events (task_id, ts, kind, message, data_json) VALUES (?,?,?,?,?)",
                (task_id, _now(), kind, message, json.dumps(data, default=str)),
            )

    def events(self, task_id: str, after: int = 0) -> list[EventRecord]:
        with closing(self._conn()) as c:
            rows = c.execute(
                "SELECT * FROM events WHERE task_id = ? AND id > ? ORDER BY id", (task_id, after)
            ).fetchall()
        return [
            EventRecord(id=r["id"], task_id=r["task_id"], ts=r["ts"], kind=r["kind"],
                        message=r["message"], data=json.loads(r["data_json"]))
            for r in rows
        ]  # fmt: skip

    def request_cancel(self, task_id: str) -> None:
        self.update(task_id, cancel_requested=1)

    def cancel_requested(self, task_id: str) -> bool:
        with closing(self._conn()) as c:
            row = c.execute(
                "SELECT cancel_requested FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
        return bool(row and row[0])

    def mark_interrupted(self) -> int:
        """Tasks left 'running' by a dead process become resumable 'interrupted' tasks."""
        with closing(self._conn()) as c, c:
            cur = c.execute(
                "UPDATE tasks SET status = 'interrupted', updated_at = ? WHERE status = 'running'",
                (_now(),),
            )
            return cur.rowcount
