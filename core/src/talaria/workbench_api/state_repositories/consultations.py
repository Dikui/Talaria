"""协同会诊及提示词仓储。"""

from __future__ import annotations

import json
import sqlite3

from talaria.foundation.collaboration import ConsultationRecord
from talaria.foundation.errors import ConsultationNotFoundError

SCHEMA = """
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
"""


class ConsultationRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    _COLLAB_SELECT = (
        "consultation_id, snapshot_id, instrument_id, timeframe, status, "
        "host_session_id, host_turn_id, created_ts, updated_ts, payload, prompt"
    )

    def create(
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

    def update(
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

    def get(self, consultation_id: str) -> ConsultationRecord:
        row = self._connection.execute(
            f"SELECT {self._COLLAB_SELECT} FROM collaborations "
            "WHERE consultation_id = ?",
            (consultation_id,),
        ).fetchone()
        if row is None:
            raise ConsultationNotFoundError(f"会诊 {consultation_id} 不存在")
        return ConsultationRecord.model_validate(json.loads(row[9]))

    def get_prompt(self, consultation_id: str) -> str:
        row = self._connection.execute(
            "SELECT prompt FROM collaborations WHERE consultation_id = ?",
            (consultation_id,),
        ).fetchone()
        if row is None:
            raise ConsultationNotFoundError(f"会诊 {consultation_id} 不存在")
        return str(row[0])

    def list(self, *, limit: int = 50) -> list[ConsultationRecord]:
        rows = self._connection.execute(
            f"""
            SELECT {self._COLLAB_SELECT} FROM collaborations
            ORDER BY created_ts DESC, consultation_id DESC LIMIT ?
            """,
            (max(1, min(limit, 200)),),
        ).fetchall()
        return [ConsultationRecord.model_validate(json.loads(row[9])) for row in rows]
