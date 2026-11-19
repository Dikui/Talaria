"""协同与感知状态的唯一写者：单线程 SQLite + 单 asyncio 协程串行队列。

架构约定（docs/architecture.md §5.2、ADR-0006）：``state/collaboration_state.sqlite``
只允许 daemon 内一个写者；写操作排队到一个协程，逐条提交，退出时刷盘并
``PRAGMA wal_checkpoint(TRUNCATE)``。05 的 watchlist / insight / staged plan
可直接复用 ``events`` 与 ``meta`` 表；任务单 05 的人工画线使用
``drawings``（当前版本）与 ``drawing_versions``（完整版本链）两张表。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, TypeVar

from talaria.foundation.alerts import (
    AlertRecord,
    AlertStatus,
    AlertTriggerRecord,
)
from talaria.foundation.annotations import (
    AgentAnnotationRecord,
    AnnotationAdoptionRecord,
    AnnotationSessionSummary,
    AnnotationStatus,
)
from talaria.foundation.collaboration import (
    ConsultationRecord,
    UsageReport,
    UsageRow,
    UsageSummary,
)
from talaria.foundation.drawings import (
    DrawingCreate,
    DrawingPatch,
    DrawingPoint,
    DrawingRecord,
    DrawingStyle,
    visible_in_window,
)
from talaria.foundation.errors import (
    AlertNotFoundError,
    AnnotationNotFoundError,
    ConsultationNotFoundError,
    DrawingNotFoundError,
)
from talaria.foundation.sqlite import (
    checkpoint_and_close,
    claim_writer,
    release_writer,
)

STATE_SUBDIR = Path("state")
STATE_DB = STATE_SUBDIR / "collaboration_state.sqlite"

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

#: 系统动态分组排在手工分组之后；前端展示时会重新编号为连续 position。
SYSTEM_GROUP_POSITION = 1_000_000
SCANNER_FOCUS_GROUP_ID = "scanner-focus"

T = TypeVar("T")


@dataclass(frozen=True)
class WatchlistGroupRecord:
    group_id: str
    name: str
    position: int
    kind: str
    is_system: bool


@dataclass(frozen=True)
class WatchlistItemRecord:
    group_id: str
    instrument_id: str
    position: int


@dataclass(frozen=True)
class WatchlistRecords:
    groups: tuple[WatchlistGroupRecord, ...]
    items: tuple[WatchlistItemRecord, ...]

    def items_for(self, group_id: str) -> tuple[WatchlistItemRecord, ...]:
        return tuple(item for item in self.items if item.group_id == group_id)


def _now_ms() -> int:
    return int(time.time() * 1000)


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


def _annotation_row(record: AgentAnnotationRecord) -> tuple[Any, ...]:
    return (
        record.annotation_id,
        record.instrument_id,
        record.snapshot_id,
        record.timeframe,
        record.session_id,
        record.round,
        record.kind.value,
        record.status.value,
        record.ts_anchor,
        record.created_ts,
        record.updated_ts,
        record.adopted_drawing_id,
        record.archived_reason,
        record.model_dump_json(),
    )


def _annotation_from_row(row: tuple[Any, ...]) -> AgentAnnotationRecord:
    return AgentAnnotationRecord.model_validate_json(row[13])


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


class StateStore:
    """唯一写者持有的 SQLite 连接；所有方法必须在同一线程/协程内串行调用。"""

    def __init__(self, data_dir: Path | str, *, now_ms: Callable[[], int] = _now_ms) -> None:
        self._db_path = Path(data_dir) / STATE_DB
        self._now_ms = now_ms
        self._closed = False
        claim_writer(self._db_path)
        try:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            self._connection = sqlite3.connect(
                self._db_path, isolation_level=None, check_same_thread=False
            )
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA synchronous=FULL")
            self._connection.execute("PRAGMA busy_timeout=5000")
            self._connection.execute("PRAGMA foreign_keys=ON")
            self._connection.executescript(
                """
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
                CREATE TABLE IF NOT EXISTS watchlist_groups (
                    group_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    position INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    is_system INTEGER NOT NULL CHECK (is_system IN (0, 1)),
                    created_ts INTEGER NOT NULL,
                    updated_ts INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS watchlist_items (
                    group_id TEXT NOT NULL,
                    instrument_id TEXT NOT NULL,
                    position INTEGER NOT NULL,
                    created_ts INTEGER NOT NULL,
                    updated_ts INTEGER NOT NULL,
                    PRIMARY KEY (group_id, instrument_id),
                    FOREIGN KEY (group_id) REFERENCES watchlist_groups(group_id)
                        ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS watchlist_items_group_idx
                    ON watchlist_items(group_id, position);
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
                CREATE TABLE IF NOT EXISTS alerts (
                    alert_id TEXT PRIMARY KEY,
                    instrument_id TEXT NOT NULL,
                    timeframe TEXT NOT NULL,
                    status TEXT NOT NULL,
                    repeat_flag INTEGER NOT NULL CHECK (repeat_flag IN (0, 1)),
                    next_arm_ts INTEGER,
                    created_ts INTEGER NOT NULL,
                    updated_ts INTEGER NOT NULL,
                    deleted_at INTEGER,
                    payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS alerts_by_instrument
                    ON alerts (instrument_id, deleted_at);
                CREATE INDEX IF NOT EXISTS alerts_by_status
                    ON alerts (status, deleted_at);
                CREATE TABLE IF NOT EXISTS alert_triggers (
                    trigger_id TEXT PRIMARY KEY,
                    alert_id TEXT NOT NULL,
                    instrument_id TEXT NOT NULL,
                    timeframe TEXT NOT NULL,
                    triggered_ts INTEGER NOT NULL,
                    snapshot_id TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_ts INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS alert_triggers_by_time
                    ON alert_triggers (triggered_ts DESC);
                CREATE INDEX IF NOT EXISTS alert_triggers_by_alert
                    ON alert_triggers (alert_id, triggered_ts DESC);
                CREATE TABLE IF NOT EXISTS agent_annotations (
                    annotation_id TEXT PRIMARY KEY,
                    instrument_id TEXT NOT NULL,
                    snapshot_id TEXT NOT NULL,
                    timeframe TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    round INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    ts_anchor INTEGER NOT NULL,
                    created_ts INTEGER NOT NULL,
                    updated_ts INTEGER NOT NULL,
                    adopted_drawing_id TEXT,
                    archived_reason TEXT,
                    payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS agent_annotations_by_instrument
                    ON agent_annotations (instrument_id, status, created_ts);
                CREATE INDEX IF NOT EXISTS agent_annotations_by_snapshot
                    ON agent_annotations (snapshot_id, status);
                CREATE INDEX IF NOT EXISTS agent_annotations_by_session
                    ON agent_annotations (session_id, status);
                CREATE TABLE IF NOT EXISTS annotation_adoptions (
                    annotation_id TEXT PRIMARY KEY,
                    instrument_id TEXT NOT NULL,
                    snapshot_id TEXT NOT NULL,
                    timeframe TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    round INTEGER NOT NULL,
                    drawing_id TEXT NOT NULL,
                    adopted_ts INTEGER NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS annotation_adoptions_by_instrument
                    ON annotation_adoptions (instrument_id, adopted_ts DESC);
                CREATE TABLE IF NOT EXISTS collaborations (
                    consultation_id TEXT PRIMARY KEY,
                    snapshot_id TEXT NOT NULL,
                    instrument_id TEXT NOT NULL,
                    timeframe TEXT NOT NULL,
                    status TEXT NOT NULL,
                    host_session_id TEXT,
                    host_turn_id TEXT,
                    created_ts INTEGER NOT NULL,
                    updated_ts INTEGER NOT NULL,
                    payload TEXT NOT NULL,
                    prompt TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS collaborations_by_time
                    ON collaborations (created_ts DESC);
                CREATE TABLE IF NOT EXISTS token_usage (
                    dsh_session_id TEXT NOT NULL,
                    dsh_turn_id TEXT NOT NULL,
                    input_tokens INTEGER,
                    cached_input_tokens INTEGER,
                    output_tokens INTEGER,
                    source TEXT NOT NULL,
                    reported_ts INTEGER NOT NULL,
                    PRIMARY KEY (dsh_session_id, dsh_turn_id)
                );
                CREATE INDEX IF NOT EXISTS token_usage_by_time
                    ON token_usage (reported_ts DESC);
                """
            )
        except BaseException:
            release_writer(self._db_path)
            raise

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

    def set_meta(self, key: str, value: Any, *, ts: int | None = None) -> None:
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

    def get_meta(self, key: str) -> Any | None:
        row = self._connection.execute(
            "SELECT value FROM meta WHERE key = ?", (key,)
        ).fetchone()
        return None if row is None else json.loads(row[0])

    # ---------------------------------------------------------------- 人工画线

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        """显式事务：当前版本与版本链必须在同一次提交里可见。"""

        self._connection.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self._connection.execute("ROLLBACK")
            raise
        else:
            self._connection.execute("COMMIT")

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

    def create_drawing(self, request: DrawingCreate, *, drawing_id: str) -> DrawingRecord:
        ts = self._now_ms()
        record = _record_from_create(request, drawing_id=drawing_id, ts=ts)
        with self._transaction():
            self._write_latest(record)
        return record

    def get_drawing(self, drawing_id: str, *, version: int | None = None) -> DrawingRecord:
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

    def list_drawing_versions(self, drawing_id: str) -> list[DrawingRecord]:
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

    def update_drawing(
        self, drawing_id: str, patch: DrawingPatch, *, ts: int | None = None
    ) -> DrawingRecord:
        current = self.get_drawing(drawing_id)
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
        with self._transaction():
            self._write_latest(updated)
        return updated

    def soft_delete_drawing(
        self, drawing_id: str, *, ts: int | None = None
    ) -> DrawingRecord:
        current = self.get_drawing(drawing_id)
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
        with self._transaction():
            self._write_latest(deleted)
        return deleted

    def restore_drawing(
        self, drawing_id: str, *, ts: int | None = None
    ) -> DrawingRecord:
        current = self.get_drawing(drawing_id)
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
        with self._transaction():
            self._write_latest(restored)
        return restored

    def list_drawings(
        self,
        instrument_id: str,
        timeframe: str,
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

    def clear_drawings(self, instrument_id: str, *, ts: int | None = None) -> int:
        rows = self._connection.execute(
            "SELECT drawing_id FROM drawings WHERE instrument_id = ? AND deleted_at IS NULL",
            (instrument_id,),
        ).fetchall()
        deleted_ts = self._now_ms() if ts is None else ts
        with self._transaction():
            for (drawing_id,) in rows:
                current = self.get_drawing(drawing_id)
                deleted = current.model_copy(
                    update={
                        "version": current.version + 1,
                        "updated_ts": deleted_ts,
                        "deleted_at": deleted_ts,
                    }
                )
                self._write_latest(deleted)
        return len(rows)
    # ------------------------------------------------------------ 智能体标注

    def _write_annotation(self, record: AgentAnnotationRecord) -> None:
        self._connection.execute(
            """
            INSERT INTO agent_annotations (
                annotation_id, instrument_id, snapshot_id, timeframe, session_id,
                round, kind, status, ts_anchor, created_ts, updated_ts,
                adopted_drawing_id, archived_reason, payload
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(annotation_id) DO UPDATE SET
                instrument_id = excluded.instrument_id,
                snapshot_id = excluded.snapshot_id,
                timeframe = excluded.timeframe,
                session_id = excluded.session_id,
                round = excluded.round,
                kind = excluded.kind,
                status = excluded.status,
                ts_anchor = excluded.ts_anchor,
                created_ts = excluded.created_ts,
                updated_ts = excluded.updated_ts,
                adopted_drawing_id = excluded.adopted_drawing_id,
                archived_reason = excluded.archived_reason,
                payload = excluded.payload
            """,
            _annotation_row(record),
        )

    def insert_annotation(
        self,
        record: AgentAnnotationRecord,
        *,
        limit: int = 12,
    ) -> tuple[AgentAnnotationRecord, list[AgentAnnotationRecord]]:
        """单事务插入；超过上限时按轮次归档最旧的未采纳标注。"""

        archived: list[AgentAnnotationRecord] = []
        with self._transaction():
            rows = self._connection.execute(
                """
                SELECT payload FROM agent_annotations
                WHERE instrument_id = ? AND status = ?
                ORDER BY round, created_ts, annotation_id
                """,
                (record.instrument_id, AnnotationStatus.ACTIVE.value),
            ).fetchall()
            overflow = len(rows) + 1 - limit
            for (payload,) in rows[: max(0, overflow)]:
                current = AgentAnnotationRecord.model_validate_json(payload)
                archived_record = current.model_copy(
                    update={
                        "status": AnnotationStatus.ARCHIVED,
                        "archived_reason": f"超过单标的 {limit} 条上限，按最旧轮次归档",
                        "updated_ts": record.created_ts,
                    }
                )
                self._write_annotation(archived_record)
                archived.append(archived_record)
            self._write_annotation(record)
        return record, archived

    def get_annotation(self, annotation_id: str) -> AgentAnnotationRecord:
        row = self._connection.execute(
            "SELECT payload FROM agent_annotations WHERE annotation_id = ?",
            (annotation_id,),
        ).fetchone()
        if row is None:
            raise AnnotationNotFoundError(f"标注 {annotation_id} 不存在")
        return AgentAnnotationRecord.model_validate_json(row[0])

    def update_annotation(
        self, record: AgentAnnotationRecord
    ) -> AgentAnnotationRecord:
        with self._transaction():
            self._write_annotation(record)
        return record

    def list_annotations(
        self,
        *,
        instrument_id: str | None = None,
        snapshot_id: str | None = None,
        session_id: str | None = None,
        include_archived: bool = False,
    ) -> list[AgentAnnotationRecord]:
        clauses = ["1 = 1"]
        params: list[Any] = []
        if instrument_id is not None:
            clauses.append("instrument_id = ?")
            params.append(instrument_id)
        if snapshot_id is not None:
            clauses.append("snapshot_id = ?")
            params.append(snapshot_id)
        if session_id is not None:
            clauses.append("session_id = ?")
            params.append(session_id)
        if not include_archived:
            clauses.append("status != ?")
            params.append(AnnotationStatus.ARCHIVED.value)
        rows = self._connection.execute(
            f"""
            SELECT payload FROM agent_annotations
            WHERE {" AND ".join(clauses)}
            ORDER BY created_ts DESC, annotation_id DESC
            """,
            params,
        ).fetchall()
        return [AgentAnnotationRecord.model_validate_json(row[0]) for row in rows]

    def clear_annotations(
        self,
        instrument_id: str,
        *,
        session_id: str | None = None,
        ts: int | None = None,
    ) -> int:
        clauses = ["instrument_id = ?", "status = ?"]
        params: list[Any] = [instrument_id, AnnotationStatus.ACTIVE.value]
        if session_id is not None:
            clauses.append("session_id = ?")
            params.append(session_id)
        rows = self._connection.execute(
            f"SELECT payload FROM agent_annotations WHERE {' AND '.join(clauses)}",
            params,
        ).fetchall()
        updated_ts = self._now_ms() if ts is None else ts
        with self._transaction():
            for (payload,) in rows:
                current = AgentAnnotationRecord.model_validate_json(payload)
                self._write_annotation(
                    current.model_copy(
                        update={
                            "status": AnnotationStatus.DISMISSED,
                            "updated_ts": updated_ts,
                        }
                    )
                )
        return len(rows)

    def record_annotation_adoption(
        self, adoption: AnnotationAdoptionRecord
    ) -> AnnotationAdoptionRecord:
        with self._transaction():
            self._connection.execute(
                """
                INSERT INTO annotation_adoptions (
                    annotation_id, instrument_id, snapshot_id, timeframe, session_id,
                    round, drawing_id, adopted_ts, payload
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(annotation_id) DO UPDATE SET
                    drawing_id = excluded.drawing_id,
                    adopted_ts = excluded.adopted_ts,
                    payload = excluded.payload
                """,
                (
                    adoption.annotation_id,
                    adoption.instrument_id,
                    adoption.snapshot_id,
                    adoption.timeframe,
                    adoption.session_id,
                    adoption.round,
                    adoption.drawing_id,
                    adoption.adopted_ts,
                    adoption.model_dump_json(),
                ),
            )
        return adoption

    def list_annotation_adoptions(self) -> list[AnnotationAdoptionRecord]:
        rows = self._connection.execute(
            "SELECT payload FROM annotation_adoptions ORDER BY adopted_ts DESC"
        ).fetchall()
        return [AnnotationAdoptionRecord.model_validate_json(row[0]) for row in rows]

    def list_annotation_sessions(self) -> list[AnnotationSessionSummary]:
        rows = self._connection.execute(
            """
            SELECT session_id, instrument_id, updated_ts FROM agent_annotations
            WHERE status != ?
            """,
            (AnnotationStatus.ARCHIVED.value,),
        ).fetchall()
        sessions: dict[str, AnnotationSessionSummary] = {}
        for session_id, instrument_id, updated_ts in rows:
            summary = sessions.get(session_id)
            if summary is None:
                summary = AnnotationSessionSummary(
                    session_id=session_id,
                    instrument_ids=[],
                    annotation_count=0,
                    latest_ts=int(updated_ts),
                )
                sessions[session_id] = summary
            summary.annotation_count += 1
            summary.latest_ts = max(summary.latest_ts, int(updated_ts))
            if instrument_id not in summary.instrument_ids:
                summary.instrument_ids.append(instrument_id)
        return sorted(
            sessions.values(),
            key=lambda item: item.latest_ts,
            reverse=True,
        )

    def list_watchlist(self) -> WatchlistRecords:
        groups = tuple(
            WatchlistGroupRecord(
                group_id=row[0],
                name=row[1],
                position=int(row[2]),
                kind=row[3],
                is_system=bool(row[4]),
            )
            for row in self._connection.execute(
                """
                SELECT group_id, name, position, kind, is_system
                FROM watchlist_groups
                ORDER BY is_system, position, group_id
                """
            )
        )
        items = tuple(
            WatchlistItemRecord(
                group_id=row[0],
                instrument_id=row[1],
                position=int(row[2]),
            )
            for row in self._connection.execute(
                """
                SELECT group_id, instrument_id, position
                FROM watchlist_items
                ORDER BY group_id, position, instrument_id
                """
            )
        )
        return WatchlistRecords(groups=groups, items=items)

    def get_watchlist_group(self, group_id: str) -> WatchlistGroupRecord | None:
        row = self._connection.execute(
            """
            SELECT group_id, name, position, kind, is_system
            FROM watchlist_groups WHERE group_id = ?
            """,
            (group_id,),
        ).fetchone()
        if row is None:
            return None
        return WatchlistGroupRecord(
            group_id=row[0],
            name=row[1],
            position=int(row[2]),
            kind=row[3],
            is_system=bool(row[4]),
        )

    def create_watchlist_group(
        self,
        name: str,
        *,
        group_id: str | None = None,
    ) -> str:
        clean_name = name.strip()
        if not clean_name:
            raise ValueError("分组名称不能为空")
        identifier = group_id or uuid.uuid4().hex
        now = self._now_ms()
        with self._transaction():
            row = self._connection.execute(
                "SELECT COALESCE(MAX(position) + 1, 0) FROM watchlist_groups "
                "WHERE is_system = 0"
            ).fetchone()
            position = int(row[0]) if row is not None else 0
            self._connection.execute(
                """
                INSERT INTO watchlist_groups
                    (group_id, name, position, kind, is_system, created_ts, updated_ts)
                VALUES (?, ?, ?, 'MANUAL', 0, ?, ?)
                """,
                (identifier, clean_name, position, now, now),
            )
        return identifier

    def rename_watchlist_group(self, group_id: str, name: str) -> None:
        clean_name = name.strip()
        if not clean_name:
            raise ValueError("分组名称不能为空")
        cursor = self._connection.execute(
            "UPDATE watchlist_groups SET name = ?, updated_ts = ? WHERE group_id = ?",
            (clean_name, self._now_ms(), group_id),
        )
        if cursor.rowcount == 0:
            raise KeyError(group_id)

    def delete_watchlist_group(self, group_id: str) -> None:
        with self._transaction():
            cursor = self._connection.execute(
                "DELETE FROM watchlist_groups WHERE group_id = ?", (group_id,)
            )
            if cursor.rowcount == 0:
                raise KeyError(group_id)
            self._connection.execute(
                "DELETE FROM watchlist_items WHERE group_id = ?", (group_id,)
            )

    def add_watchlist_item(self, group_id: str, instrument_id: str) -> None:
        now = self._now_ms()
        with self._transaction():
            row = self._connection.execute(
                "SELECT COALESCE(MAX(position) + 1, 0) FROM watchlist_items "
                "WHERE group_id = ?",
                (group_id,),
            ).fetchone()
            position = int(row[0]) if row is not None else 0
            try:
                self._connection.execute(
                    """
                    INSERT INTO watchlist_items
                        (group_id, instrument_id, position, created_ts, updated_ts)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (group_id, instrument_id, position, now, now),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError("标的已在分组中") from exc

    def remove_watchlist_item(self, group_id: str, instrument_id: str) -> None:
        cursor = self._connection.execute(
            "DELETE FROM watchlist_items WHERE group_id = ? AND instrument_id = ?",
            (group_id, instrument_id),
        )
        if cursor.rowcount == 0:
            raise KeyError(instrument_id)

    def reorder_watchlist_groups(self, ids: list[str]) -> None:
        """只调整手工分组顺序；系统分组位置保持不变。"""
        now = self._now_ms()
        with self._transaction():
            for position, group_id in enumerate(ids):
                cursor = self._connection.execute(
                    """
                    UPDATE watchlist_groups
                    SET position = ?, updated_ts = ?
                    WHERE group_id = ? AND is_system = 0
                    """,
                    (position, now, group_id),
                )
                if cursor.rowcount == 0:
                    raise KeyError(group_id)

    def reorder_watchlist_items(self, group_id: str, ids: list[str]) -> None:
        now = self._now_ms()
        with self._transaction():
            for position, instrument_id in enumerate(ids):
                cursor = self._connection.execute(
                    """
                    UPDATE watchlist_items
                    SET position = ?, updated_ts = ?
                    WHERE group_id = ? AND instrument_id = ?
                    """,
                    (position, now, group_id, instrument_id),
                )
                if cursor.rowcount == 0:
                    raise KeyError(instrument_id)

    def upsert_system_group(
        self,
        name: str,
        instrument_ids: list[str],
        *,
        group_id: str = SCANNER_FOCUS_GROUP_ID,
    ) -> None:
        """供扫描器（任务单 15）写入动态分组；本单只读展示。"""
        clean_name = name.strip()
        if not clean_name:
            raise ValueError("分组名称不能为空")
        now = self._now_ms()
        wanted = list(dict.fromkeys(instrument_ids))
        with self._transaction():
            existing = self.get_watchlist_group(group_id)
            if existing is None:
                row = self._connection.execute(
                    "SELECT COUNT(*) FROM watchlist_groups WHERE is_system = 1"
                ).fetchone()
                position = SYSTEM_GROUP_POSITION + (int(row[0]) if row is not None else 0)
                self._connection.execute(
                    """
                    INSERT INTO watchlist_groups
                        (group_id, name, position, kind, is_system, created_ts, updated_ts)
                    VALUES (?, ?, ?, 'SCANNER_FOCUS', 1, ?, ?)
                    """,
                    (group_id, clean_name, position, now, now),
                )
            else:
                self._connection.execute(
                    """
                    UPDATE watchlist_groups
                    SET name = ?, kind = 'SCANNER_FOCUS', is_system = 1, updated_ts = ?
                    WHERE group_id = ?
                    """,
                    (clean_name, now, group_id),
                )
            if wanted:
                placeholders = ",".join("?" for _ in wanted)
                self._connection.execute(
                    f"DELETE FROM watchlist_items WHERE group_id = ? "
                    f"AND instrument_id NOT IN ({placeholders})",
                    (group_id, *wanted),
                )
            else:
                self._connection.execute(
                    "DELETE FROM watchlist_items WHERE group_id = ?", (group_id,)
                )
            for position, instrument_id in enumerate(wanted):
                self._connection.execute(
                    """
                    INSERT INTO watchlist_items
                        (group_id, instrument_id, position, created_ts, updated_ts)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(group_id, instrument_id) DO UPDATE SET
                        position = excluded.position, updated_ts = excluded.updated_ts
                    """,
                    (group_id, instrument_id, position, now, now),
                )

    def list_key_level_ignores(self, instrument_id: str) -> list[dict[str, object]]:
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

    def set_key_level_ignore(
        self,
        instrument_id: str,
        level_id: str,
        ignored: bool,
        *,
        ts: int | None = None,
    ) -> None:
        now = self._now_ms() if ts is None else ts
        with self._transaction():
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
            self.append_event(
                "key_level.ignore",
                {
                    "instrument_id": instrument_id,
                    "level_id": level_id,
                    "ignored": ignored,
                },
                ts=now,
            )

    def list_key_level_adoptions(
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

    def get_key_level_adoption(
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

    def record_key_level_adoption(
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
        with self._transaction():
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
            self.append_event(
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

    # ------------------------------------------------------------------ 预警

    #: 规则行只额外保存可过滤列，完整契约仍以 JSON payload 为准。
    _ALERT_SELECT = (
        "alert_id, instrument_id, timeframe, status, repeat_flag, next_arm_ts, "
        "created_ts, updated_ts, deleted_at, payload"
    )

    def write_alert(self, record: AlertRecord) -> AlertRecord:
        self._connection.execute(
            f"""
            INSERT INTO alerts ({self._ALERT_SELECT})
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, COALESCE(?, NULL), ?)
            ON CONFLICT(alert_id) DO UPDATE SET
                instrument_id = excluded.instrument_id,
                timeframe = excluded.timeframe,
                status = excluded.status,
                repeat_flag = excluded.repeat_flag,
                next_arm_ts = excluded.next_arm_ts,
                created_ts = excluded.created_ts,
                updated_ts = excluded.updated_ts,
                deleted_at = COALESCE(excluded.deleted_at, alerts.deleted_at),
                payload = excluded.payload
            """,
            (
                record.alert_id,
                record.instrument_id,
                record.timeframe,
                record.status.value,
                1 if record.repeat else 0,
                record.next_arm_ts,
                record.created_ts,
                record.updated_ts,
                None,
                json.dumps(record.model_dump(mode="json"), ensure_ascii=False, sort_keys=True),
            ),
        )
        return record

    def get_alert(self, alert_id: str, *, include_deleted: bool = False) -> AlertRecord:
        row = self._connection.execute(
            f"SELECT {self._ALERT_SELECT} FROM alerts WHERE alert_id = ?",
            (alert_id,),
        ).fetchone()
        if row is None or (row[8] is not None and not include_deleted):
            raise AlertNotFoundError(f"预警 {alert_id} 不存在")
        return AlertRecord.model_validate(json.loads(row[9]))

    def list_alerts(
        self,
        *,
        status: AlertStatus | None = None,
        include_deleted: bool = False,
    ) -> list[AlertRecord]:
        clauses = [] if include_deleted else ["deleted_at IS NULL"]
        params: list[Any] = []
        if status is not None:
            clauses.append("status = ?")
            params.append(status.value)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._connection.execute(
            f"""
            SELECT {self._ALERT_SELECT} FROM alerts {where}
            ORDER BY created_ts DESC, alert_id DESC
            """,
            params,
        ).fetchall()
        return [AlertRecord.model_validate(json.loads(row[9])) for row in rows]

    def soft_delete_alert(self, alert_id: str, *, ts: int | None = None) -> AlertRecord:
        record = self.get_alert(alert_id)
        deleted_ts = self._now_ms() if ts is None else ts
        self._connection.execute(
            "UPDATE alerts SET deleted_at = ?, updated_ts = ? WHERE alert_id = ?",
            (deleted_ts, deleted_ts, alert_id),
        )
        return record

    def append_alert_trigger(self, trigger: AlertTriggerRecord) -> AlertTriggerRecord:
        self._connection.execute(
            """
            INSERT INTO alert_triggers
                (trigger_id, alert_id, instrument_id, timeframe, triggered_ts,
                 snapshot_id, payload, created_ts)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                trigger.trigger_id,
                trigger.alert_id,
                trigger.instrument_id,
                trigger.timeframe,
                trigger.triggered_ts,
                trigger.snapshot_id,
                json.dumps(trigger.model_dump(mode="json"), ensure_ascii=False, sort_keys=True),
                self._now_ms(),
            ),
        )
        return trigger

    def list_alert_triggers(
        self,
        *,
        alert_id: str | None = None,
        instrument_id: str | None = None,
        limit: int = 200,
    ) -> list[AlertTriggerRecord]:
        clauses: list[str] = []
        params: list[Any] = []
        if alert_id is not None:
            clauses.append("alert_id = ?")
            params.append(alert_id)
        if instrument_id is not None:
            clauses.append("instrument_id = ?")
            params.append(instrument_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(max(1, min(limit, 500)))
        rows = self._connection.execute(
            f"""
            SELECT payload FROM alert_triggers {where}
            ORDER BY triggered_ts DESC, trigger_id DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
        return [AlertTriggerRecord.model_validate(json.loads(row[0])) for row in rows]

    # ---------------------------------------------------------------- 协同会诊

    _COLLAB_SELECT = (
        "consultation_id, snapshot_id, instrument_id, timeframe, status, "
        "host_session_id, host_turn_id, created_ts, updated_ts, payload, prompt"
    )

    def create_consultation(
        self,
        record: ConsultationRecord,
        *,
        prompt: str,
    ) -> ConsultationRecord:
        self._connection.execute(
            """
            INSERT INTO collaborations
                (consultation_id, snapshot_id, instrument_id, timeframe, status,
                 host_session_id, host_turn_id, created_ts, updated_ts, payload,
                 prompt)
            VALUES (?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?, ?)
            """,
            (
                record.consultation_id,
                record.snapshot_id,
                record.instrument_id,
                record.timeframe,
                record.status.value,
                record.created_ts,
                record.updated_ts,
                json.dumps(
                    record.model_dump(mode="json"),
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                prompt,
            ),
        )
        return record

    def update_consultation(
        self,
        record: ConsultationRecord,
        *,
        host_session_id: str | None = None,
        host_turn_id: str | None = None,
    ) -> ConsultationRecord:
        record = record.model_copy(
            update={
                "dsh_session_id": record.dsh_session_id or host_session_id,
                "dsh_turn_id": record.dsh_turn_id or host_turn_id,
            }
        )
        self._connection.execute(
            """
            UPDATE collaborations SET
                status = ?, host_session_id = COALESCE(?, host_session_id),
                host_turn_id = COALESCE(?, host_turn_id), updated_ts = ?,
                payload = ?
            WHERE consultation_id = ?
            """,
            (
                record.status.value,
                host_session_id,
                host_turn_id,
                record.updated_ts,
                json.dumps(
                    record.model_dump(mode="json"),
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                record.consultation_id,
            ),
        )
        return record

    def get_consultation(self, consultation_id: str) -> ConsultationRecord:
        row = self._connection.execute(
            f"SELECT {self._COLLAB_SELECT} FROM collaborations "
            "WHERE consultation_id = ?",
            (consultation_id,),
        ).fetchone()
        if row is None:
            raise ConsultationNotFoundError(f"会诊 {consultation_id} 不存在")
        return ConsultationRecord.model_validate(json.loads(row[9]))

    def get_consultation_prompt(self, consultation_id: str) -> str:
        row = self._connection.execute(
            "SELECT prompt FROM collaborations WHERE consultation_id = ?",
            (consultation_id,),
        ).fetchone()
        if row is None:
            raise ConsultationNotFoundError(f"会诊 {consultation_id} 不存在")
        return str(row[0])

    def list_consultations(self, *, limit: int = 50) -> list[ConsultationRecord]:
        rows = self._connection.execute(
            f"""
            SELECT {self._COLLAB_SELECT} FROM collaborations
            ORDER BY created_ts DESC, consultation_id DESC LIMIT ?
            """,
            (max(1, min(limit, 200)),),
        ).fetchall()
        return [ConsultationRecord.model_validate(json.loads(row[9])) for row in rows]

    # ---------------------------------------------------------------- Token 用量

    def record_usage(self, report: UsageReport, *, ts: int | None = None) -> bool:
        """按 ``(dsh_session_id, dsh_turn_id)`` 幂等入账；重复轮次返回 False。

        缺失的计数字段保持 NULL，绝不写成 0；已经入账的完整轮次不会被后续
        重复上报覆盖。唯一例外是原先标记为 ``missing`` 的轮次：宿主重试或
        轨迹补扫找到用量后，允许用完整数据补上，避免“先报缺失就永远丢失”。
        """

        cursor = self._connection.execute(
            """
            INSERT INTO token_usage
                (dsh_session_id, dsh_turn_id, input_tokens, cached_input_tokens,
                 output_tokens, source, reported_ts)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(dsh_session_id, dsh_turn_id) DO UPDATE SET
                input_tokens = excluded.input_tokens,
                cached_input_tokens = excluded.cached_input_tokens,
                output_tokens = excluded.output_tokens,
                source = excluded.source,
                reported_ts = excluded.reported_ts
            WHERE token_usage.source = 'missing'
              AND excluded.source != 'missing'
            """,
            (
                report.dsh_session_id,
                report.dsh_turn_id,
                report.input_tokens,
                report.cached_input_tokens,
                report.output_tokens,
                report.source,
                self._now_ms() if ts is None else ts,
            ),
        )
        return cursor.rowcount > 0

    def list_usage(self, *, limit: int = 200) -> list[UsageRow]:
        rows = self._connection.execute(
            """
            SELECT dsh_session_id, dsh_turn_id, input_tokens, cached_input_tokens,
                   output_tokens, source, reported_ts
            FROM token_usage ORDER BY reported_ts DESC, dsh_turn_id DESC LIMIT ?
            """,
            (max(1, min(limit, 1000)),),
        ).fetchall()
        return [
            UsageRow(
                dsh_session_id=row[0],
                dsh_turn_id=row[1],
                input_tokens=row[2],
                cached_input_tokens=row[3],
                output_tokens=row[4],
                source=row[5],
                reported_ts=row[6],
            )
            for row in rows
        ]

    def summarize_usage(self) -> tuple[UsageSummary, list[UsageSummary]]:
        rows = self._connection.execute(
            """
            SELECT dsh_session_id, input_tokens, cached_input_tokens,
                   output_tokens, reported_ts, source
            FROM token_usage
            """
        ).fetchall()
        total = UsageSummary()
        by_session: dict[str, UsageSummary] = {}
        missing = 0
        for session_id, input_tokens, cached, output, reported_ts, source in rows:
            if source == "missing":
                missing += 1
                continue
            summary = by_session.get(session_id)
            if summary is None:
                summary = UsageSummary(dsh_session_id=session_id)
                by_session[session_id] = summary
            summary.input_tokens += input_tokens or 0
            summary.cached_input_tokens += cached or 0
            summary.output_tokens += output or 0
            summary.turns += 1
            summary.updated_ts = max(summary.updated_ts, reported_ts)
            total.input_tokens += input_tokens or 0
            total.cached_input_tokens += cached or 0
            total.output_tokens += output or 0
            total.turns += 1
            total.updated_ts = max(total.updated_ts, reported_ts)
        total.sessions = len(by_session)
        total.missing_reports = missing
        return total, sorted(
            by_session.values(),
            key=lambda item: item.updated_ts,
            reverse=True,
        )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            checkpoint_and_close(self._connection)
        finally:
            release_writer(self._db_path)


class _Request:
    __slots__ = ("operation", "future")

    def __init__(
        self,
        operation: Callable[[StateStore], Any],
        future: asyncio.Future[Any],
    ) -> None:
        self.operation = operation
        self.future = future



class StateWriterQueue:
    """把状态写操作汇聚到单协程；调用者等待对应 future，保证提交顺序与失败可见。"""

    def __init__(
        self,
        data_dir: Path | str,
        *,
        now_ms: Callable[[], int] = _now_ms,
    ) -> None:
        self._store = StateStore(data_dir, now_ms=now_ms)
        self._queue: asyncio.Queue[_Request | object] = asyncio.Queue()
        self._sentinel = object()
        self._task: asyncio.Task[None] | None = None
        self._closed = False

    async def start(self) -> None:
        if self._closed:
            raise RuntimeError("StateWriterQueue 已关闭")
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    async def _submit(self, operation: Callable[[StateStore], Any]) -> Any:
        if self._closed:
            raise RuntimeError("StateWriterQueue 已关闭")
        if self._task is None:
            raise RuntimeError("StateWriterQueue 尚未启动")
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._queue.put_nowait(_Request(operation, future))
        return await future

    async def submit(
        self,
        kind: str,
        payload: Mapping[str, Any],
        *,
        ts: int | None = None,
    ) -> int:
        return await self._submit(
            lambda store: store.append_event(kind, payload, ts=ts)
        )

    async def set_meta(self, key: str, value: Any, *, ts: int | None = None) -> None:
        await self._submit(lambda store: store.set_meta(key, value, ts=ts))

    async def get_meta(self, key: str) -> Any | None:
        return await self._submit(lambda store: store.get_meta(key))

    async def run(self, operation: Callable[[StateStore], T]) -> T:
        """在单写协程上执行任意 Store 操作；读写都排队，避免跨连接并发。"""

        return await self._submit(operation)

    async def _run(self) -> None:
        try:
            while True:
                request = await self._queue.get()
                try:
                    if request is self._sentinel:
                        return
                    assert isinstance(request, _Request)
                    try:
                        result = request.operation(self._store)
                    except Exception as error:  # 交给调用者
                        if not request.future.cancelled():
                            request.future.set_exception(error)
                    else:
                        if not request.future.cancelled():
                            request.future.set_result(result)
                finally:
                    self._queue.task_done()
        finally:
            self._store.close()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._task is None:
            self._store.close()
            return
        await self._queue.join()
        self._queue.put_nowait(self._sentinel)
        await self._task

    async def __aenter__(self) -> StateWriterQueue:
        await self.start()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()
