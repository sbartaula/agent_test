"""SQLite run history (stdlib only). LangGraph checkpointing arrives in a later session."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    model TEXT NOT NULL,
    issue TEXT NOT NULL,
    plan_json TEXT NOT NULL,
    prompt_tokens INTEGER NOT NULL,
    completion_tokens INTEGER NOT NULL,
    cost_usd REAL NOT NULL
)
"""

FIX_SCHEMA = """
CREATE TABLE IF NOT EXISTS fix_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    issue TEXT NOT NULL,
    repo_path TEXT NOT NULL,
    status TEXT NOT NULL,
    result_json TEXT NOT NULL
)
"""


class RunRecord(BaseModel):
    id: int
    created_at: str
    model: str
    issue: str
    plan_json: str
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float


class RunStore:
    def __init__(self, path: str | Path) -> None:
        self._path = str(path)
        if self._path != ":memory:":
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn, conn:
            conn.execute(SCHEMA)
            conn.execute(FIX_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._path)

    def save(
        self,
        *,
        model: str,
        issue: str,
        plan_json: str,
        prompt_tokens: int,
        completion_tokens: int,
        cost_usd: float,
    ) -> int:
        with closing(self._connect()) as conn, conn:
            cur = conn.execute(
                "INSERT INTO runs (created_at, model, issue, plan_json, prompt_tokens,"
                " completion_tokens, cost_usd) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    datetime.now(UTC).isoformat(),
                    model,
                    issue,
                    plan_json,
                    prompt_tokens,
                    completion_tokens,
                    cost_usd,
                ),
            )
            return int(cur.lastrowid or 0)

    def get(self, run_id: int) -> RunRecord | None:
        with closing(self._connect()) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return RunRecord(**dict(row)) if row else None

    def save_fix(self, *, issue: str, repo_path: str, status: str, result_json: str) -> int:
        with closing(self._connect()) as conn, conn:
            cur = conn.execute(
                "INSERT INTO fix_runs (created_at, issue, repo_path, status, result_json)"
                " VALUES (?, ?, ?, ?, ?)",
                (datetime.now(UTC).isoformat(), issue, repo_path, status, result_json),
            )
            return int(cur.lastrowid or 0)

    def get_fix(self, fix_id: int) -> str | None:
        """Return the stored FixResult JSON, or None."""
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT result_json FROM fix_runs WHERE id = ?", (fix_id,)
            ).fetchone()
        return str(row[0]) if row else None
