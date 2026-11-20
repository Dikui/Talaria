"""关键位忽略及采纳记录仓储。"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable

from talaria.workbench_api.state_repositories._sqlite import write_transaction
from talaria.workbench_api.state_repositories.meta import MetaRepository

SCHEMA = """
CREATE TABLE IF NOT EXISTS key_level_ignores (
    instrument_id TEXT NOT NULL,
    level_id TEXT NOT NULL,
    ignored INTEGER NOT NULL CHECK (ignored IN (0, 1)),
    updated_ts INTEGER NOT NULL,
    PRIMARY KEY (instrument_id, level_id)
);
CREATE INDEX IF NOT EXISTS key_level_ignores_by_instrument
    ON key_level_ignores (instrument_id, updated_ts);
CREATE TABLE IF NOT EXISTS key_level_adoptions (
    instrument_id TEXT NOT NULL,
    level_id TEXT NOT NULL,
    snapshot_id TEXT NOT NULL,
    drawing_id TEXT NOT NULL,
    adopted_ts INTEGER NOT NULL,
    timeframe TEXT NOT NULL,
    PRIMARY KEY (instrument_id, level_id)
);
CREATE INDEX IF NOT EXISTS key_level_adoptions_by_instrument
    ON key_level_adoptions (instrument_id, adopted_ts DESC);
"""


class KeyLevelFeedbackRepository:
    def __init__(
        self,
        connection: sqlite3.Connection,
        now_ms: Callable[[], int],
        meta: MetaRepository,
    ) -> None:
        self._connection = connection
        self._now_ms = now_ms
        # 反馈及其事件使用同一个连接，在同一事务提交。
        self._meta = meta

    def list_ignores(self, instrument_id: str) -> list[dict[str, object]]:
        rows = self._connection.execute(
            """
            SELECT instrument_id, level_id, ignored, updated_ts
            FROM key_level_ignores
            WHERE instrument_id = ?
            ORDER BY updated_ts DESC, level_id
            """,
            (instrument_id,),
        ).fetchall()
        return [
            {
                "instrument_id": str(row[0]),
                "level_id": str(row[1]),
                "ignored": bool(row[2]),
                "updated_ts": int(row[3]),
            }
            for row in rows
        ]

    def set_ignore(
        self,
        instrument_id: str,
        level_id: str,
        ignored: bool,
        *,
        ts: int | None = None,
    ) -> None:
        now = self._now_ms() if ts is None else ts
        with write_transaction(self._connection):
            self._connection.execute(
                """
                INSERT INTO key_level_ignores
                    (instrument_id, level_id, ignored, updated_ts)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(instrument_id, level_id) DO UPDATE SET
                    ignored = excluded.ignored,
                    updated_ts = excluded.updated_ts
                """,
                (instrument_id, level_id, 1 if ignored else 0, now),
            )
            self._meta.append_event(
                "key_level.ignore",
                {
                    "instrument_id": instrument_id,
                    "level_id": level_id,
                    "ignored": ignored,
                },
                ts=now,
            )

    def list_adoptions(
        self, instrument_id: str
    ) -> list[dict[str, object]]:
        rows = self._connection.execute(
            """
            SELECT instrument_id, level_id, snapshot_id, drawing_id,
                   adopted_ts, timeframe
            FROM key_level_adoptions
            WHERE instrument_id = ?
            ORDER BY adopted_ts DESC, level_id
            """,
            (instrument_id,),
        ).fetchall()
        return [
            {
                "instrument_id": str(row[0]),
                "level_id": str(row[1]),
                "snapshot_id": str(row[2]),
                "drawing_id": str(row[3]),
                "adopted_ts": int(row[4]),
                "timeframe": str(row[5]),
            }
            for row in rows
        ]

    def get_adoption(
        self, instrument_id: str, level_id: str
    ) -> dict[str, object] | None:
        row = self._connection.execute(
            """
            SELECT instrument_id, level_id, snapshot_id, drawing_id,
                   adopted_ts, timeframe
            FROM key_level_adoptions
            WHERE instrument_id = ? AND level_id = ?
            """,
            (instrument_id, level_id),
        ).fetchone()
        if row is None:
            return None
        return {
            "instrument_id": str(row[0]),
            "level_id": str(row[1]),
            "snapshot_id": str(row[2]),
            "drawing_id": str(row[3]),
            "adopted_ts": int(row[4]),
            "timeframe": str(row[5]),
        }

    def record_adoption(
        self,
        *,
        instrument_id: str,
        level_id: str,
        snapshot_id: str,
        drawing_id: str,
        timeframe: str,
        ts: int | None = None,
    ) -> None:
        now = self._now_ms() if ts is None else ts
        with write_transaction(self._connection):
            self._connection.execute(
                """
                INSERT INTO key_level_adoptions
                    (instrument_id, level_id, snapshot_id, drawing_id,
                     adopted_ts, timeframe)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(instrument_id, level_id) DO UPDATE SET
                    snapshot_id = excluded.snapshot_id,
                    drawing_id = excluded.drawing_id,
                    adopted_ts = excluded.adopted_ts,
                    timeframe = excluded.timeframe
                """,
                (instrument_id, level_id, snapshot_id, drawing_id, now, timeframe),
            )
            self._meta.append_event(
                "key_level.adopt",
                {
                    "instrument_id": instrument_id,
                    "level_id": level_id,
                    "snapshot_id": snapshot_id,
                    "drawing_id": drawing_id,
                    "timeframe": timeframe,
                },
                ts=now,
            )
