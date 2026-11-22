"""人工画线及其版本链仓储。"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from decimal import Decimal
from typing import Any

from talaria.foundation.drawings import (
    DrawingCreate,
    DrawingPatch,
    DrawingPoint,
    DrawingRecord,
    DrawingStyle,
    visible_in_window,
)
from talaria.foundation.errors import DrawingNotFoundError
from talaria.workbench_api.state_repositories._sqlite import write_transaction

SCHEMA = """
CREATE TABLE IF NOT EXISTS drawings (
    drawing_id TEXT PRIMARY KEY,
    instrument_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    visible_timeframes TEXT NOT NULL,
    points TEXT NOT NULL,
    text TEXT NOT NULL,
    style TEXT NOT NULL,
    version INTEGER NOT NULL,
    created_ts INTEGER NOT NULL,
    updated_ts INTEGER NOT NULL,
    deleted_at INTEGER
);
CREATE INDEX IF NOT EXISTS drawings_by_instrument
    ON drawings (instrument_id, deleted_at);
CREATE TABLE IF NOT EXISTS drawing_versions (
    drawing_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    instrument_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    visible_timeframes TEXT NOT NULL,
    points TEXT NOT NULL,
    text TEXT NOT NULL,
    style TEXT NOT NULL,
    created_ts INTEGER NOT NULL,
    updated_ts INTEGER NOT NULL,
    deleted_at INTEGER,
    PRIMARY KEY (drawing_id, version)
);
"""


#: ``drawings`` 与 ``drawing_versions`` 共用的列顺序，读取时按同一顺序解包。
_DRAWING_COLUMNS = (
    "drawing_id",
    "instrument_id",
    "kind",
    "visible_timeframes",
    "points",
    "text",
    "style",
    "version",
    "created_ts",
    "updated_ts",
    "deleted_at",
)
_DRAWING_SELECT = ", ".join(_DRAWING_COLUMNS)


def _drawing_row(record: DrawingRecord) -> tuple[Any, ...]:
    """把契约对象编码成 SQLite 行；价格保持十进制字符串，不写 JSON number。"""

    return (
        record.drawing_id,
        record.instrument_id,
        record.kind.value,
        json.dumps(record.visible_timeframes, ensure_ascii=False),
        json.dumps(
            [point.model_dump(mode="json") for point in record.points],
            ensure_ascii=False,
        ),
        record.text,
        json.dumps(record.style.model_dump(mode="json"), ensure_ascii=False),
        record.version,
        record.created_ts,
        record.updated_ts,
        record.deleted_at,
    )


def _drawing_from_row(row: tuple[Any, ...]) -> DrawingRecord:
    (
        drawing_id,
        instrument_id,
        kind,
        visible_timeframes,
        points,
        text,
        style,
        version,
        created_ts,
        updated_ts,
        deleted_at,
    ) = row
    return DrawingRecord(
        drawing_id=drawing_id,
        instrument_id=instrument_id,
        kind=kind,
        visible_timeframes=json.loads(visible_timeframes),
        points=[DrawingPoint(**item) for item in json.loads(points)],
        text=text,
        style=DrawingStyle(**json.loads(style)),
        version=version,
        created_ts=created_ts,
        updated_ts=updated_ts,
        deleted_at=deleted_at,
    )


def _record_from_create(
    request: DrawingCreate, *, drawing_id: str, ts: int
) -> DrawingRecord:
    return DrawingRecord(
        drawing_id=drawing_id,
        instrument_id=request.instrument_id,
        kind=request.kind,
        visible_timeframes=list(request.visible_timeframes),
        points=request.points,
        text=request.text,
        style=request.style,
        version=1,
        created_ts=ts,
        updated_ts=ts,
    )


class DrawingRepository:
    def __init__(
        self, connection: sqlite3.Connection, now_ms: Callable[[], int]
    ) -> None:
        self._connection = connection
        self._now_ms = now_ms

    def _insert_drawing_version(self, record: DrawingRecord) -> None:
        self._connection.execute(
            f"""
            INSERT INTO drawing_versions ({_DRAWING_SELECT})
            VALUES ({", ".join("?" for _ in _DRAWING_COLUMNS)})
            """,
            _drawing_row(record),
        )

    def _write_latest(self, record: DrawingRecord) -> None:
        self._connection.execute(
            f"""
            INSERT INTO drawings ({_DRAWING_SELECT})
            VALUES ({", ".join("?" for _ in _DRAWING_COLUMNS)})
            ON CONFLICT(drawing_id) DO UPDATE SET
                instrument_id = excluded.instrument_id,
                kind = excluded.kind,
                visible_timeframes = excluded.visible_timeframes,
                points = excluded.points,
                text = excluded.text,
                style = excluded.style,
                version = excluded.version,
                created_ts = excluded.created_ts,
                updated_ts = excluded.updated_ts,
                deleted_at = excluded.deleted_at
            """,
            _drawing_row(record),
        )
        self._insert_drawing_version(record)

    def create(self, request: DrawingCreate, *, drawing_id: str) -> DrawingRecord:
        ts = self._now_ms()
        record = _record_from_create(request, drawing_id=drawing_id, ts=ts)
        with write_transaction(self._connection):
            self._write_latest(record)
        return record

    def get(self, drawing_id: str, *, version: int | None = None) -> DrawingRecord:
        if version is None:
            row = self._connection.execute(
                f"SELECT {_DRAWING_SELECT} FROM drawings WHERE drawing_id = ?",
                (drawing_id,),
            ).fetchone()
        else:
            row = self._connection.execute(
                f"""
                SELECT {_DRAWING_SELECT} FROM drawing_versions
                WHERE drawing_id = ? AND version = ?
                """,
                (drawing_id, version),
            ).fetchone()
        if row is None:
            raise DrawingNotFoundError(f"画线 {drawing_id} 不存在")
        return _drawing_from_row(row)

    def list_versions(self, drawing_id: str) -> list[DrawingRecord]:
        rows = self._connection.execute(
            f"""
            SELECT {_DRAWING_SELECT} FROM drawing_versions
            WHERE drawing_id = ? ORDER BY version
            """,
            (drawing_id,),
        ).fetchall()
        if not rows:
            raise DrawingNotFoundError(f"画线 {drawing_id} 不存在")
        return [_drawing_from_row(row) for row in rows]

    def update(
        self, drawing_id: str, patch: DrawingPatch, *, ts: int | None = None
    ) -> DrawingRecord:
        current = self.get(drawing_id)
        if current.deleted_at is not None:
            raise DrawingNotFoundError(f"画线 {drawing_id} 已删除")
        updated_ts = self._now_ms() if ts is None else ts
        updated = current.model_copy(
            update={
                "visible_timeframes": (
                    current.visible_timeframes
                    if patch.visible_timeframes is None
                    else patch.visible_timeframes
                ),
                "points": current.points if patch.points is None else patch.points,
                "text": current.text if patch.text is None else patch.text,
                "style": current.style if patch.style is None else patch.style,
                "version": current.version + 1,
                "updated_ts": updated_ts,
            }
        )
        with write_transaction(self._connection):
            self._write_latest(updated)
        return updated

    def soft_delete(
        self, drawing_id: str, *, ts: int | None = None
    ) -> DrawingRecord:
        current = self.get(drawing_id)
        if current.deleted_at is not None:
            return current
        deleted_ts = self._now_ms() if ts is None else ts
        deleted = current.model_copy(
            update={
                "version": current.version + 1,
                "updated_ts": deleted_ts,
                "deleted_at": deleted_ts,
            }
        )
        with write_transaction(self._connection):
            self._write_latest(deleted)
        return deleted

    def restore(
        self, drawing_id: str, *, ts: int | None = None
    ) -> DrawingRecord:
        current = self.get(drawing_id)
        if current.deleted_at is None:
            return current
        restored_ts = self._now_ms() if ts is None else ts
        restored = current.model_copy(
            update={
                "version": current.version + 1,
                "updated_ts": restored_ts,
                "deleted_at": None,
            }
        )
        with write_transaction(self._connection):
            self._write_latest(restored)
        return restored

    def list(
        self,
        instrument_id: str,
        timeframe: str | None,
        *,
        from_ts: int | None = None,
        to_ts: int | None = None,
        price_min: Decimal | None = None,
        price_max: Decimal | None = None,
    ) -> list[DrawingRecord]:
        rows = self._connection.execute(
            f"""
            SELECT {_DRAWING_SELECT} FROM drawings
            WHERE instrument_id = ? AND deleted_at IS NULL
            ORDER BY created_ts, drawing_id
            """,
            (instrument_id,),
        ).fetchall()
        records = [_drawing_from_row(row) for row in rows]
        return [
            record
            for record in records
            if visible_in_window(
                record,
                timeframe=timeframe,
                from_ts=from_ts,
                to_ts=to_ts,
                price_min=price_min,
                price_max=price_max,
            )
        ]

    def clear(self, instrument_id: str, *, ts: int | None = None) -> int:
        rows = self._connection.execute(
            "SELECT drawing_id FROM drawings WHERE instrument_id = ? AND deleted_at IS NULL",
            (instrument_id,),
        ).fetchall()
        deleted_ts = self._now_ms() if ts is None else ts
        with write_transaction(self._connection):
            for (drawing_id,) in rows:
                current = self.get(drawing_id)
                deleted = current.model_copy(
                    update={
                        "version": current.version + 1,
                        "updated_ts": deleted_ts,
                        "deleted_at": deleted_ts,
                    }
                )
                self._write_latest(deleted)
        return len(rows)
