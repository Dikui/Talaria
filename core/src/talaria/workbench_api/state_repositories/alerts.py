"""盘面预警规则及触发记录仓储。"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from typing import Any

from talaria.foundation.alerts import AlertRecord, AlertStatus, AlertTriggerRecord
from talaria.foundation.errors import AlertNotFoundError

SCHEMA = """
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
"""


class AlertRepository:
    def __init__(
        self, connection: sqlite3.Connection, now_ms: Callable[[], int]
    ) -> None:
        self._connection = connection
        self._now_ms = now_ms

    #: 规则行只额外保存可过滤列，完整契约仍以 JSON payload 为准。
    _ALERT_SELECT = (
        "alert_id, instrument_id, timeframe, status, repeat_flag, next_arm_ts, "
        "created_ts, updated_ts, deleted_at, payload"
    )

    def write(self, record: AlertRecord) -> AlertRecord:
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

    def get(self, alert_id: str, *, include_deleted: bool = False) -> AlertRecord:
        row = self._connection.execute(
            f"SELECT {self._ALERT_SELECT} FROM alerts WHERE alert_id = ?",
            (alert_id,),
        ).fetchone()
        if row is None or (row[8] is not None and not include_deleted):
            raise AlertNotFoundError(f"预警 {alert_id} 不存在")
        return AlertRecord.model_validate(json.loads(row[9]))

    def list(
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

    def soft_delete(self, alert_id: str, *, ts: int | None = None) -> AlertRecord:
        record = self.get(alert_id)
        deleted_ts = self._now_ms() if ts is None else ts
        self._connection.execute(
            "UPDATE alerts SET deleted_at = ?, updated_ts = ? WHERE alert_id = ?",
            (deleted_ts, deleted_ts, alert_id),
        )
        return record

    def append_trigger(self, trigger: AlertTriggerRecord) -> AlertTriggerRecord:
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

    def list_triggers(
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
