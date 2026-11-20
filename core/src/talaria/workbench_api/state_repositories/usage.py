"""Token 用量记录及汇总仓储。"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable

from talaria.foundation.collaboration import UsageReport, UsageRow, UsageSummary

SCHEMA = """
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


class UsageRepository:
    def __init__(
        self, connection: sqlite3.Connection, now_ms: Callable[[], int]
    ) -> None:
        self._connection = connection
        self._now_ms = now_ms

    def record(self, report: UsageReport, *, ts: int | None = None) -> bool:
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

    def list(self, *, limit: int = 200) -> list[UsageRow]:
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

    def summarize(self) -> tuple[UsageSummary, list[UsageSummary]]:
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
