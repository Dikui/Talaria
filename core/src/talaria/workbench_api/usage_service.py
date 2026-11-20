"""宿主 Token 用量的入账与汇总。"""

from __future__ import annotations

from collections.abc import Callable

from talaria.foundation.collaboration import (
    UsageReport,
    UsageReportResponse,
    UsageRow,
    UsageSummary,
    UsageSummaryResponse,
)
from talaria.workbench_api.state_store import StateStore, StateWriterQueue


class UsageService:
    def __init__(self, queue: StateWriterQueue) -> None:
        self._queue = queue

    async def record(
        self, report: UsageReport, *, now_ms: Callable[[], int] | None = None
    ) -> bool:
        return await self._queue.run(
            lambda store: store.usage.record(
                report, ts=None if now_ms is None else now_ms()
            )
        )

    async def report(self, report: UsageReport) -> UsageReportResponse:
        recorded = await self.record(report)
        return UsageReportResponse(
            recorded=recorded,
            dsh_session_id=report.dsh_session_id,
            dsh_turn_id=report.dsh_turn_id,
            duplicate=not recorded,
        )

    async def summary(self, *, limit: int = 100) -> UsageSummaryResponse:
        def operation(
            store: StateStore,
        ) -> tuple[UsageSummary, list[UsageSummary], list[UsageRow]]:
            total, sessions = store.usage.summarize()
            return total, sessions, store.usage.list(limit=limit)

        total, sessions, recent = await self._queue.read(operation)
        return UsageSummaryResponse(summary=total, sessions=sessions, recent=recent)
