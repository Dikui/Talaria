"""人工画线服务：把 HTTP 参数校验与单写队列串起来。

所有落库操作都通过 ``StateWriterQueue.run`` 在 daemon 的专用写线程上执行：
HTTP 处理协程只负责校验参数、等待 future，不在事件循环里直接碰 SQLite。
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from decimal import Decimal

from talaria.foundation.drawings import (
    KIND_POINT_COUNTS,
    ChartView,
    ChartViewWindow,
    DrawingCreate,
    DrawingKind,
    DrawingListResponse,
    DrawingPatch,
    DrawingPoint,
    DrawingRecord,
    DrawingVersionList,
)
from talaria.foundation.errors import (
    InstrumentNotFoundError,
    InvalidRequestError,
)
from talaria.foundation.market import MarketPack
from talaria.foundation.timeframes import SUPPORTED_TIMEFRAMES
from talaria.workbench_api.state_store import StateWriterQueue


def _check_timeframe(timeframe: str) -> None:
    if timeframe not in SUPPORTED_TIMEFRAMES:
        supported = "、".join(SUPPORTED_TIMEFRAMES)
        raise InvalidRequestError(f"不支持的周期 {timeframe!r}；可选：{supported}")


def _check_visible_timeframes(timeframes: list[str]) -> None:
    for timeframe in timeframes:
        _check_timeframe(timeframe)
    if len(set(timeframes)) != len(timeframes):
        raise InvalidRequestError("visible_timeframes 不能包含重复周期")


def _check_points(kind: DrawingKind, points: list[DrawingPoint], text: str) -> None:
    expected = KIND_POINT_COUNTS[kind]
    if len(points) != expected:
        raise InvalidRequestError(
            f"{kind.value} 需要 {expected} 个锚点，收到 {len(points)} 个"
        )
    if kind is DrawingKind.TEXT_LABEL and not text.strip():
        raise InvalidRequestError("TEXT_LABEL 的 text 不能为空")


def _check_window(from_ts: int, to_ts: int | None) -> None:
    if to_ts is not None and to_ts < from_ts:
        raise InvalidRequestError("to_ts 不能早于 from_ts")


class DrawingService:
    """画线用例层；只依赖市场包与单写队列。"""

    def __init__(
        self,
        *,
        queue: StateWriterQueue,
        pack: MarketPack,
        now_ms: Callable[[], int],
    ) -> None:
        self._queue = queue
        self._pack = pack
        self._now_ms = now_ms

    def _instrument(self, instrument_id: str):
        spec = self._pack.instrument(instrument_id)
        if spec is None:
            raise InstrumentNotFoundError(f"未知标的 {instrument_id}")
        return spec

    async def list_drawings(
        self,
        instrument_id: str,
        timeframe: str,
        *,
        all_timeframes: bool = False,
        from_ts: int | None = None,
        to_ts: int | None = None,
    ) -> DrawingListResponse:
        self._instrument(instrument_id)
        _check_timeframe(timeframe)
        _check_window(from_ts or 0, to_ts)
        drawings = await self._queue.read(
            lambda store: store.drawings.list(
                instrument_id, None if all_timeframes else timeframe, from_ts=from_ts, to_ts=to_ts
            )
        )
        return DrawingListResponse(
            instrument_id=instrument_id, timeframe=timeframe, drawings=drawings
        )

    async def get_chart_view(
        self,
        instrument_id: str,
        timeframe: str,
        *,
        from_ts: int,
        to_ts: int,
        price_min: Decimal,
        price_max: Decimal,
    ) -> ChartView:
        spec = self._instrument(instrument_id)
        _check_timeframe(timeframe)
        _check_window(from_ts, to_ts)
        if not price_min.is_finite() or not price_max.is_finite():
            raise InvalidRequestError("price_min/price_max 必须是有限十进制数")
        if price_min > price_max:
            raise InvalidRequestError("price_min 不能大于 price_max")
        drawings = await self._queue.read(
            lambda store: store.drawings.list(
                instrument_id,
                timeframe,
                from_ts=from_ts,
                to_ts=to_ts,
                price_min=price_min,
                price_max=price_max,
            )
        )
        return ChartView(
            instrument=spec,
            timeframe=timeframe,
            window=ChartViewWindow(
                from_ts=from_ts,
                to_ts=to_ts,
                price_min=price_min,
                price_max=price_max,
            ),
            drawings=drawings,
        )

    async def create_drawing(self, request: DrawingCreate) -> DrawingRecord:
        self._instrument(request.instrument_id)
        _check_visible_timeframes(request.visible_timeframes)
        _check_points(request.kind, request.points, request.text)
        drawing_id = f"drw_{uuid.uuid4().hex}"
        return await self._queue.run(
            lambda store: store.drawings.create(request, drawing_id=drawing_id)
        )

    async def patch_drawing(
        self, drawing_id: str, patch: DrawingPatch
    ) -> DrawingRecord:
        if patch.visible_timeframes is not None:
            _check_visible_timeframes(patch.visible_timeframes)

        def operation(store):
            current = store.drawings.get(drawing_id)
            _check_points(
                current.kind,
                current.points if patch.points is None else patch.points,
                current.text if patch.text is None else patch.text,
            )
            return store.drawings.update(drawing_id, patch)

        return await self._queue.run(operation)

    async def delete_drawing(self, drawing_id: str) -> DrawingRecord:
        return await self._queue.run(
            lambda store: store.drawings.soft_delete(drawing_id)
        )

    async def restore_drawing(self, drawing_id: str) -> DrawingRecord:
        return await self._queue.run(
            lambda store: store.drawings.restore(drawing_id)
        )

    async def get_drawing(
        self,
        drawing_id: str,
        *,
        version: int | None = None,
        after_pending_writes: bool = False,
    ) -> DrawingRecord:
        """预警挂载校验可在已排队的画线写入之后读取。"""

        read = self._queue.run if after_pending_writes else self._queue.read
        return await read(
            lambda store: store.drawings.get(drawing_id, version=version)
        )

    async def list_versions(self, drawing_id: str) -> DrawingVersionList:
        versions = await self._queue.read(
            lambda store: store.drawings.list_versions(drawing_id)
        )
        return DrawingVersionList(drawing_id=drawing_id, versions=versions)

    async def clear_drawings(self, instrument_id: str) -> int:
        self._instrument(instrument_id)
        return await self._queue.run(
            lambda store: store.drawings.clear(instrument_id)
        )
