"""元数据与事件仓储。"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Mapping
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_ts INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    ts INTEGER NOT NULL
);
"""


class MetaRepository:
    def __init__(
        self, connection: sqlite3.Connection, now_ms: Callable[[], int]
    ) -> None:
        self._connection = connection
        self._now_ms = now_ms

    def append_event(
        self,
        kind: str,
        payload: Mapping[str, Any],
        *,
        ts: int | None = None,
    ) -> int:
        cursor = self._connection.execute(
            "INSERT INTO events (kind, payload, ts) VALUES (?, ?, ?)",
            (
                kind,
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
                self._now_ms() if ts is None else ts,
            ),
        )
        return int(cursor.lastrowid or 0)

    def set(self, key: str, value: Any, *, ts: int | None = None) -> None:
        self._connection.execute(
            """
            INSERT INTO meta (key, value, updated_ts) VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value, updated_ts = excluded.updated_ts
            """,
            (
                key,
                json.dumps(value, ensure_ascii=False, sort_keys=True),
                self._now_ms() if ts is None else ts,
            ),
        )

    def get(self, key: str) -> Any | None:
        row = self._connection.execute(
            "SELECT value FROM meta WHERE key = ?", (key,)
        ).fetchone()
        return None if row is None else json.loads(row[0])
