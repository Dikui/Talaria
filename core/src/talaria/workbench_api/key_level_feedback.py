"""关键位反馈：忽略/恢复与“一键采纳”为独立人工画线。

采纳写入两处：``drawings`` 表（通过 DrawingService，与系统关键位解耦）和
``key_level_adoptions`` 来源记录；两者的写入都在 ``StateWriterQueue`` 的专用线程上执行。
"""

from __future__ import annotations

import time
from collections.abc import Callable

from talaria.foundation.drawings import (
    DrawingCreate,
    DrawingKind,
    DrawingLineStyle,
    DrawingPoint,
    DrawingRecord,
    DrawingStyle,
)
from talaria.foundation.errors import DrawingNotFoundError, InvalidRequestError
from talaria.foundation.key_levels import (
    KeyLevel,
    KeyLevelAdoptionRecord,
    KeyLevelFeedbackSummary,
    KeyLevelIgnoreList,
    KeyLevelIgnoreRecord,
    KeyLevelIgnoreRequest,
)
from talaria.workbench_api.drawing_service import DrawingService
from talaria.workbench_api.state_store import StateWriterQueue


def _now_ms() -> int:
    return int(time.time() * 1000)


class KeyLevelFeedbackService:
    def __init__(
        self,
        *,
        state_writer: StateWriterQueue,
        drawings: DrawingService,
        now_ms: Callable[[], int] = _now_ms,
    ) -> None:
        self._state_writer = state_writer
        self._drawings = drawings
        self._now_ms = now_ms

    async def list(self, instrument_id: str) -> KeyLevelIgnoreList:
        rows = await self._state_writer.read(
            lambda store: store.list_key_level_ignores(instrument_id)
        )
        return KeyLevelIgnoreList(
            instrument_id=instrument_id,
            entries=[KeyLevelIgnoreRecord(**row) for row in rows],
        )

    async def set_ignored(
        self, request: KeyLevelIgnoreRequest
    ) -> KeyLevelIgnoreList:
        if not request.instrument_id or not request.level_id:
            raise InvalidRequestError("instrument_id 与 level_id 不能为空")
        await self._state_writer.run(
            lambda store: store.set_key_level_ignore(
                request.instrument_id, request.level_id, request.ignored
            )
        )
        return await self.list(request.instrument_id)

    async def adopt(
        self,
        level: KeyLevel,
        *,
        snapshot_id: str,
        timeframe: str,
    ) -> DrawingRecord:
        """把系统关键位复制为 HUMAN_DRAWING；已采纳且画线未删除时幂等返回。"""

        if not snapshot_id:
            raise InvalidRequestError("snapshot_id 不能为空")
        existing = await self._state_writer.run(
            lambda store: store.get_key_level_adoption(
                level.instrument_id, level.level_id
            )
        )
        if existing is not None:
            try:
                drawing = await self._drawings.get_drawing(
                    str(existing["drawing_id"])
                )
            except DrawingNotFoundError:
                drawing = None
            if drawing is not None and drawing.deleted_at is None:
                return drawing

        request = self._drawing_from_level(level, timeframe=timeframe)
        drawing = await self._drawings.create_drawing(request)
        await self._state_writer.run(
            lambda store: store.record_key_level_adoption(
                instrument_id=level.instrument_id,
                level_id=level.level_id,
                snapshot_id=snapshot_id,
                drawing_id=drawing.drawing_id,
                timeframe=timeframe,
            )
        )
        return drawing

    async def adoption(
        self, instrument_id: str, level_id: str
    ) -> KeyLevelAdoptionRecord | None:
        row = await self._state_writer.read(
            lambda store: store.get_key_level_adoption(instrument_id, level_id)
        )
        return None if row is None else KeyLevelAdoptionRecord(**row)

    async def feedback(self, instrument_id: str) -> KeyLevelFeedbackSummary:
        ignores, adoptions = await self._state_writer.read(
            lambda store: (
                store.list_key_level_ignores(instrument_id),
                store.list_key_level_adoptions(instrument_id),
            )
        )
        return KeyLevelFeedbackSummary(
            instrument_id=instrument_id,
            adopted_level_ids=[str(row["level_id"]) for row in adoptions],
            ignored_level_ids=[
                str(row["level_id"]) for row in ignores if row["ignored"]
            ],
        )

    def _drawing_from_level(
        self, level: KeyLevel, *, timeframe: str
    ) -> DrawingCreate:
        text = f"来源关键位 {level.level_id}（{level.kind.value}）"
        if level.price is not None:
            return DrawingCreate(
                instrument_id=level.instrument_id,
                kind=DrawingKind.HORIZONTAL_LINE,
                visible_timeframes=[timeframe],
                points=[DrawingPoint(ts=0, price=level.price)],
                text=text,
                style=DrawingStyle(
                    line_width=2, line_style=DrawingLineStyle.DASHED
                ),
            )

        assert level.price_low is not None and level.price_high is not None
        end_ts = max(level.ts_available, level.formed_ts + 1, self._now_ms())
        return DrawingCreate(
            instrument_id=level.instrument_id,
            kind=DrawingKind.RECTANGLE,
            visible_timeframes=[timeframe],
            points=[
                DrawingPoint(ts=level.formed_ts, price=level.price_low),
                DrawingPoint(ts=end_ts, price=level.price_high),
            ],
            text=text,
            style=DrawingStyle(
                line_width=2,
                line_style=DrawingLineStyle.DASHED,
                fill_opacity=0.06,
            ),
        )
