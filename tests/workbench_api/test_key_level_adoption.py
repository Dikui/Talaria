"""关键位采纳：复制为独立 HUMAN_DRAWING，并持久化采纳/忽略反馈。"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from pathlib import Path

import pytest

from talaria.foundation.drawings import DrawingKind
from talaria.foundation.key_levels import (
    EvidenceClass,
    KeyLevel,
    KeyLevelCategory,
    KeyLevelIgnoreRequest,
    KeyLevelKind,
    KeyLevelStatus,
)
from talaria.packs.crypto_perp import CRYPTO_PERP_PACK
from talaria.workbench_api.drawing_service import DrawingService
from talaria.workbench_api.key_level_feedback import KeyLevelFeedbackService
from talaria.workbench_api.state_store import StateWriterQueue

pytestmark = pytest.mark.medium

INSTRUMENT = "BTC-USDT-PERP"
TIMEFRAME = "1m"
NOW_MS = 1_795_022_974_103
LEVEL_ID = "kl_0123456789abcdef"


class Clock:
    def __init__(self, now: int = NOW_MS) -> None:
        self.now = now

    def __call__(self) -> int:
        return self.now


def _level(
    *,
    price: str | None = "101.5",
    low: str | None = None,
    high: str | None = None,
    kind: KeyLevelKind = KeyLevelKind.PREV_DAY_HIGH,
) -> KeyLevel:
    return KeyLevel(
        level_id=LEVEL_ID,
        instrument_id=INSTRUMENT,
        kind=kind,
        category=KeyLevelCategory.SESSION,
        evidence_class=EvidenceClass.OBSERVED,
        timeframe=TIMEFRAME,
        price=Decimal(price) if price is not None else None,
        price_low=Decimal(low) if low is not None else None,
        price_high=Decimal(high) if high is not None else None,
        strength=0.8,
        status=KeyLevelStatus.ACTIVE,
        ts_available=NOW_MS - 60_000,
        formed_ts=NOW_MS - 300_000,
        touch_count=0,
        volume_weight="1",
    )


async def _make(
    tmp_path: Path, clock: Clock
) -> tuple[StateWriterQueue, DrawingService, KeyLevelFeedbackService]:
    writer = StateWriterQueue(tmp_path / "data", now_ms=clock)
    await writer.start()
    drawings = DrawingService(queue=writer, pack=CRYPTO_PERP_PACK, now_ms=clock)
    feedback = KeyLevelFeedbackService(
        state_writer=writer, drawings=drawings, now_ms=clock
    )
    return writer, drawings, feedback


def test_adopt_creates_independent_horizontal_drawing(tmp_path: Path) -> None:
    async def scenario() -> None:
        clock = Clock()
        writer, drawings, feedback = await _make(tmp_path, clock)
        try:
            level = _level()
            drawing = await feedback.adopt(
                level, snapshot_id="snap_a", timeframe=TIMEFRAME
            )
            assert drawing.kind is DrawingKind.HORIZONTAL_LINE
            assert drawing.instrument_id == INSTRUMENT
            assert drawing.visible_timeframes == [TIMEFRAME]
            assert len(drawing.points) == 1
            assert drawing.points[0].price == Decimal("101.5")
            assert LEVEL_ID in drawing.text
            assert drawing.text.startswith("来源关键位")

            listing = await drawings.list_drawings(INSTRUMENT, TIMEFRAME)
            assert [item.drawing_id for item in listing.drawings] == [
                drawing.drawing_id
            ]

            row = await writer.run(
                lambda store: store.get_key_level_adoption(INSTRUMENT, LEVEL_ID)
            )
            assert row is not None
            assert row["drawing_id"] == drawing.drawing_id
            assert row["snapshot_id"] == "snap_a"
            assert row["timeframe"] == TIMEFRAME
            assert row["adopted_ts"] == NOW_MS

            # 关键位变化后画线保持独立，不再跟随系统位。
            level.price = Decimal("99")
            level.status = KeyLevelStatus.BROKEN
            stored = await drawings.get_drawing(drawing.drawing_id)
            assert stored.points[0].price == Decimal("101.5")
        finally:
            await writer.close()

    asyncio.run(scenario())


def test_adopt_is_idempotent_until_drawing_is_deleted(tmp_path: Path) -> None:
    async def scenario() -> None:
        clock = Clock()
        writer, drawings, feedback = await _make(tmp_path, clock)
        try:
            level = _level()
            first = await feedback.adopt(
                level, snapshot_id="snap_a", timeframe=TIMEFRAME
            )
            again = await feedback.adopt(
                level, snapshot_id="snap_b", timeframe=TIMEFRAME
            )
            assert again.drawing_id == first.drawing_id

            await drawings.delete_drawing(first.drawing_id)
            replacement = await feedback.adopt(
                level, snapshot_id="snap_b", timeframe=TIMEFRAME
            )
            assert replacement.drawing_id != first.drawing_id
            row = await writer.run(
                lambda store: store.get_key_level_adoption(INSTRUMENT, LEVEL_ID)
            )
            assert row is not None
            assert row["drawing_id"] == replacement.drawing_id
            assert row["snapshot_id"] == "snap_b"
        finally:
            await writer.close()

    asyncio.run(scenario())


def test_adopt_zone_creates_rectangle_spanning_low_and_high(tmp_path: Path) -> None:
    async def scenario() -> None:
        clock = Clock()
        writer, _, feedback = await _make(tmp_path, clock)
        try:
            level = _level(
                price=None,
                low="100",
                high="102",
                kind=KeyLevelKind.SESSION_HIGH,
            )
            drawing = await feedback.adopt(
                level, snapshot_id="snap_zone", timeframe=TIMEFRAME
            )
            assert drawing.kind is DrawingKind.RECTANGLE
            assert [point.price for point in drawing.points] == [
                Decimal("100"),
                Decimal("102"),
            ]
            assert drawing.points[0].ts < drawing.points[1].ts
            assert drawing.points[1].ts >= NOW_MS - 60_000
            assert LEVEL_ID in drawing.text
            assert drawing.style.fill_opacity > 0
        finally:
            await writer.close()

    asyncio.run(scenario())


def test_feedback_summarizes_adopted_and_ignored(tmp_path: Path) -> None:
    async def scenario() -> None:
        clock = Clock()
        writer, _, feedback = await _make(tmp_path, clock)
        try:
            await feedback.adopt(_level(), snapshot_id="snap_a", timeframe=TIMEFRAME)
            await feedback.set_ignored(
                KeyLevelIgnoreRequest(
                    instrument_id=INSTRUMENT, level_id="kl_ignored", ignored=True
                )
            )
            summary = await feedback.feedback(INSTRUMENT)
            assert summary.instrument_id == INSTRUMENT
            assert summary.adopted_level_ids == [LEVEL_ID]
            assert summary.ignored_level_ids == ["kl_ignored"]

            await feedback.set_ignored(
                KeyLevelIgnoreRequest(
                    instrument_id=INSTRUMENT, level_id="kl_ignored", ignored=False
                )
            )
            summary = await feedback.feedback(INSTRUMENT)
            assert summary.ignored_level_ids == []
        finally:
            await writer.close()

    asyncio.run(scenario())


def test_adoption_persists_across_state_writer_restart(tmp_path: Path) -> None:
    async def scenario() -> None:
        clock = Clock()
        writer, _, feedback = await _make(tmp_path, clock)
        drawing_id = (
            await feedback.adopt(_level(), snapshot_id="snap_a", timeframe=TIMEFRAME)
        ).drawing_id
        await writer.close()

        restarted = StateWriterQueue(tmp_path / "data", now_ms=clock)
        await restarted.start()
        try:
            row = await restarted.run(
                lambda store: store.get_key_level_adoption(INSTRUMENT, LEVEL_ID)
            )
            assert row is not None
            assert row["drawing_id"] == drawing_id
            summary = await KeyLevelFeedbackService(
                state_writer=restarted,
                drawings=DrawingService(
                    queue=restarted, pack=CRYPTO_PERP_PACK, now_ms=clock
                ),
                now_ms=clock,
            ).feedback(INSTRUMENT)
            assert summary.adopted_level_ids == [LEVEL_ID]
        finally:
            await restarted.close()

    asyncio.run(scenario())
