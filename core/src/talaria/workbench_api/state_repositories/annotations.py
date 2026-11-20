"""智能体标注及采纳记录仓储。"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from typing import Any

from talaria.foundation.annotations import (
    AgentAnnotationRecord,
    AnnotationAdoptionRecord,
    AnnotationSessionSummary,
    AnnotationStatus,
)
from talaria.foundation.errors import AnnotationNotFoundError
from talaria.workbench_api.state_repositories._sqlite import write_transaction

SCHEMA = """
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
"""


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


class AnnotationRepository:
    def __init__(
        self, connection: sqlite3.Connection, now_ms: Callable[[], int]
    ) -> None:
        self._connection = connection
        self._now_ms = now_ms

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

    def insert(
        self,
        record: AgentAnnotationRecord,
        *,
        limit: int = 12,
    ) -> tuple[AgentAnnotationRecord, list[AgentAnnotationRecord]]:
        """单事务插入；超过上限时按轮次归档最旧的未采纳标注。"""

        archived: list[AgentAnnotationRecord] = []
        with write_transaction(self._connection):
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

    def get(self, annotation_id: str) -> AgentAnnotationRecord:
        row = self._connection.execute(
            "SELECT payload FROM agent_annotations WHERE annotation_id = ?",
            (annotation_id,),
        ).fetchone()
        if row is None:
            raise AnnotationNotFoundError(f"标注 {annotation_id} 不存在")
        return AgentAnnotationRecord.model_validate_json(row[0])

    def update(
        self, record: AgentAnnotationRecord
    ) -> AgentAnnotationRecord:
        with write_transaction(self._connection):
            self._write_annotation(record)
        return record

    def list(
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

    def clear(
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
        with write_transaction(self._connection):
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

    def record_adoption(
        self, adoption: AnnotationAdoptionRecord
    ) -> AnnotationAdoptionRecord:
        with write_transaction(self._connection):
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

    def list_adoptions(self) -> list[AnnotationAdoptionRecord]:
        rows = self._connection.execute(
            "SELECT payload FROM annotation_adoptions ORDER BY adopted_ts DESC"
        ).fetchall()
        return [AnnotationAdoptionRecord.model_validate_json(row[0]) for row in rows]

    def list_sessions(self) -> list[AnnotationSessionSummary]:
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
