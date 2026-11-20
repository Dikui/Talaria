"""人工画线版本链及并发更新的持久化。"""

from __future__ import annotations

import asyncio
import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest

from talaria.foundation.drawings import DrawingCreate, DrawingPatch, DrawingPoint
from talaria.workbench_api.state_store import STATE_DB, StateWriterQueue

pytestmark = [pytest.mark.medium, pytest.mark.spec]

NOW_MS = 1_795_022_974_103


def _point(ts: int, price: str) -> DrawingPoint:
    return DrawingPoint(ts=ts, price=price)


def _drawing() -> DrawingCreate:
    return DrawingCreate(
        instrument_id="BTC-USDT-PERP",
        kind="SEGMENT",
        visible_timeframes=["1h"],
        points=[_point(1_000, "101.10"), _point(2_000, "105.25")],
    )


def test_drawing_version_chain_round_trips_through_queue(tmp_path: Path) -> None:
    async def scenario() -> list[int]:
        queue = StateWriterQueue(tmp_path, now_ms=lambda: NOW_MS)
        await queue.start()
        try:
            created = await queue.run(
                lambda store: store.drawings.create(_drawing(), drawing_id="drw_q1")
            )
            patched = await queue.run(
                lambda store: store.drawings.update(
                    "drw_q1", DrawingPatch(points=[_point(2_000, "102.00")])
                )
            )
            await queue.run(lambda store: store.drawings.soft_delete("drw_q1"))
            restored = await queue.run(
                lambda store: store.drawings.restore("drw_q1")
            )
            versions = await queue.run(
                lambda store: store.drawings.list_versions("drw_q1")
            )
            assert created.version == 1
            assert patched.version == 2
            assert restored.version == 4
            assert [item.version for item in versions] == [1, 2, 3, 4]
            assert versions[0].points[0].price == Decimal("101.10")
            return [item.version for item in versions]
        finally:
            await queue.close()

    assert asyncio.run(scenario()) == [1, 2, 3, 4]

    connection = sqlite3.connect(tmp_path / STATE_DB)
    try:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        points_json = connection.execute(
            "SELECT points FROM drawing_versions WHERE version = 1"
        ).fetchone()[0]
    finally:
        connection.close()
    assert {"drawings", "drawing_versions"} <= tables
    # SQLite 中价格必须是带引号的字符串，不能退化成 JSON number。
    assert '"price": "101.10"' in points_json


def test_concurrent_drawing_updates_are_serialized(tmp_path: Path) -> None:
    async def scenario() -> int:
        queue = StateWriterQueue(tmp_path, now_ms=lambda: NOW_MS)
        await queue.start()
        try:
            await queue.run(
                lambda store: store.drawings.create(_drawing(), drawing_id="drw_q2")
            )
            patches = [
                DrawingPatch(text=f"v{index}")
                for index in range(6)
            ]
            await asyncio.gather(
                *(
                    queue.run(
                        lambda store, patch=patch: store.drawings.update(
                            "drw_q2", patch
                        )
                    )
                    for patch in patches
                )
            )
            latest = await queue.run(lambda store: store.drawings.get("drw_q2"))
            return latest.version
        finally:
            await queue.close()

    assert asyncio.run(scenario()) == 7
