"""扫描简报及已读状态仓储。"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from talaria.foundation.briefings import BriefingContent, BriefingStatus, ScannerBriefing
from talaria.foundation.collaboration import EvidenceRef
from talaria.foundation.errors import BriefingNotFoundError

SCHEMA = """
CREATE TABLE IF NOT EXISTS scanner_briefings (
    id TEXT PRIMARY KEY,
    trigger_id TEXT NOT NULL,
    snapshot_id TEXT NOT NULL,
    instrument TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    status TEXT NOT NULL,
    summary TEXT,
    evidence_refs TEXT NOT NULL,
    abstain_reason TEXT,
    created_ts INTEGER NOT NULL,
    read_ts INTEGER
);
CREATE INDEX IF NOT EXISTS scanner_briefings_by_time
    ON scanner_briefings (created_ts DESC, id DESC);
CREATE INDEX IF NOT EXISTS scanner_briefings_unread
    ON scanner_briefings (read_ts, created_ts DESC);
"""


class BriefingRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    _BRIEFING_SELECT = (
        "id, trigger_id, snapshot_id, instrument, timeframe, status, summary, "
        "evidence_refs, abstain_reason, created_ts, read_ts"
    )

    def _briefing_from_row(self, row: tuple[Any, ...]) -> ScannerBriefing:
        summary = (
            BriefingContent.model_validate(json.loads(row[6])) if row[6] else None
        )
        evidence = [
            EvidenceRef.model_validate(item) for item in json.loads(row[7])
        ]
        return ScannerBriefing(
            id=row[0],
            trigger_id=row[1],
            snapshot_id=row[2],
            instrument_id=row[3],
            timeframe=row[4],
            status=BriefingStatus(row[5]),
            summary=summary,
            evidence_refs=evidence,
            abstain_reason=row[8],
            created_ts=row[9],
            read_ts=row[10],
        )

    def create(self, record: ScannerBriefing) -> ScannerBriefing:
        self._connection.execute(
            """
            INSERT INTO scanner_briefings
                (id, trigger_id, snapshot_id, instrument, timeframe, status,
                 summary, evidence_refs, abstain_reason, created_ts, read_ts)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.id,
                record.trigger_id,
                record.snapshot_id,
                record.instrument_id,
                record.timeframe,
                record.status.value,
                (
                    json.dumps(
                        record.summary.model_dump(mode="json"),
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                    if record.summary is not None
                    else None
                ),
                json.dumps(
                    [item.model_dump(mode="json") for item in record.evidence_refs],
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                record.abstain_reason,
                record.created_ts,
                record.read_ts,
            ),
        )
        return record

    def update(self, record: ScannerBriefing) -> ScannerBriefing:
        self._connection.execute(
            """
            UPDATE scanner_briefings SET
                status = ?, summary = ?, evidence_refs = ?, abstain_reason = ?,
                read_ts = ?
            WHERE id = ?
            """,
            (
                record.status.value,
                (
                    json.dumps(
                        record.summary.model_dump(mode="json"),
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                    if record.summary is not None
                    else None
                ),
                json.dumps(
                    [item.model_dump(mode="json") for item in record.evidence_refs],
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                record.abstain_reason,
                record.read_ts,
                record.id,
            ),
        )
        return record

    def get(self, briefing_id: str) -> ScannerBriefing:
        row = self._connection.execute(
            f"SELECT {self._BRIEFING_SELECT} FROM scanner_briefings WHERE id = ?",
            (briefing_id,),
        ).fetchone()
        if row is None:
            raise BriefingNotFoundError(f"简报 {briefing_id} 不存在")
        return self._briefing_from_row(row)

    def list(
        self, *, limit: int = 50, unread_only: bool = False
    ) -> list[ScannerBriefing]:
        where = "WHERE read_ts IS NULL" if unread_only else ""
        rows = self._connection.execute(
            f"""
            SELECT {self._BRIEFING_SELECT} FROM scanner_briefings {where}
            ORDER BY created_ts DESC, rowid DESC LIMIT ?
            """,
            (max(1, min(limit, 200)),),
        ).fetchall()
        return [self._briefing_from_row(row) for row in rows]

    def count_unread(self) -> int:
        row = self._connection.execute(
            "SELECT COUNT(*) FROM scanner_briefings WHERE read_ts IS NULL"
        ).fetchone()
        return int(row[0]) if row is not None else 0

    def mark_read(self, briefing_id: str, ts: int) -> ScannerBriefing:
        cursor = self._connection.execute(
            "UPDATE scanner_briefings SET read_ts = COALESCE(read_ts, ?) WHERE id = ?",
            (ts, briefing_id),
        )
        if cursor.rowcount == 0:
            raise BriefingNotFoundError(f"简报 {briefing_id} 不存在")
        return self.get(briefing_id)
