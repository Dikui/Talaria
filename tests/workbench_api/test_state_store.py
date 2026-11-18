"""协同状态单写者：WAL、串行提交、关闭刷盘与 checkpoint。

来源：任务单 06、docs/architecture.md §5.2 与 ADR-0006。测试只通过公开的
``StateWriterQueue``/``StateStore`` 边界以及关闭后的 SQLite 文件观察行为。
"""

from __future__ import annotations

import asyncio
import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest

from talaria.foundation.drawings import DrawingCreate, DrawingPatch, DrawingPoint
from talaria.foundation.errors import SingleWriterViolationError
from talaria.workbench_api.state_store import StateStore, StateWriterQueue

pytestmark = [pytest.mark.medium, pytest.mark.spec]

NOW_MS = 1_795_022_974_103


def _db_path(data_dir: Path) -> Path:
    return data_dir / "state" / "collaboration_state.sqlite"


def _point(ts: int, price: str) -> DrawingPoint:
    return DrawingPoint(ts=ts, price=price)


def _drawing() -> DrawingCreate:
    return DrawingCreate(
        instrument_id="BTC-USDT-PERP",
        kind="SEGMENT",
        visible_timeframes=["1h"],
        points=[_point(1_000, "101.10"), _point(2_000, "105.25")],
    )


def test_events_are_committed_in_submission_order(tmp_path: Path) -> None:
    async def scenario() -> list[int]:
        queue = StateWriterQueue(tmp_path, now_ms=lambda: NOW_MS)
        await queue.start()
        try:
            return await asyncio.gather(
                *(queue.submit("watchlist.added", {"symbol": f"S{i}"}) for i in range(5))
            )
        finally:
            await queue.close()

    event_ids = asyncio.run(scenario())
    assert event_ids == sorted(event_ids)

    connection = sqlite3.connect(_db_path(tmp_path))
    try:
        rows = connection.execute(
            "SELECT event_id, kind, payload FROM events ORDER BY event_id"
        ).fetchall()
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    finally:
        connection.close()
    assert {"meta", "events"} <= tables
    assert [row[1] for row in rows] == ["watchlist.added"] * 5
    assert '"S4"' in rows[-1][2]


def test_close_flushes_unfinished_submissions_and_truncates_wal(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        queue = StateWriterQueue(tmp_path, now_ms=lambda: NOW_MS)
        await queue.start()
        tasks = [
            asyncio.create_task(queue.submit("note", {"index": index}))
            for index in range(10)
        ]
        await asyncio.sleep(0)
        assert not any(task.done() for task in tasks)
        await queue.close()
        assert all(task.done() for task in tasks)

    asyncio.run(scenario())

    wal = Path(f"{_db_path(tmp_path)}-wal")
    assert not wal.exists() or wal.stat().st_size == 0
    connection = sqlite3.connect(_db_path(tmp_path))
    try:
        assert connection.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 10
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        connection.close()


def test_same_process_second_writer_is_rejected(tmp_path: Path) -> None:
    async def scenario() -> None:
        first = StateWriterQueue(tmp_path, now_ms=lambda: NOW_MS)
        try:
            with pytest.raises(SingleWriterViolationError):
                StateWriterQueue(tmp_path, now_ms=lambda: NOW_MS)
        finally:
            await first.close()

    asyncio.run(scenario())


def test_submit_after_close_is_rejected(tmp_path: Path) -> None:
    async def scenario() -> None:
        queue = StateWriterQueue(tmp_path, now_ms=lambda: NOW_MS)
        await queue.start()
        await queue.close()
        with pytest.raises(RuntimeError):
            await queue.submit("note", {})

    asyncio.run(scenario())


def test_meta_values_are_upserted(tmp_path: Path) -> None:
    store = StateStore(tmp_path, now_ms=lambda: NOW_MS)
    try:
        store.set_meta("desk", {"layout": "a"})
        store.set_meta("desk", {"layout": "b"})
    finally:
        store.close()

    connection = sqlite3.connect(_db_path(tmp_path))
    try:
        rows = connection.execute("SELECT key, value FROM meta").fetchall()
    finally:
        connection.close()
    assert rows == [("desk", '{"layout": "b"}')]


def test_drawing_version_chain_round_trips_through_queue(tmp_path: Path) -> None:
    async def scenario() -> list[int]:
        queue = StateWriterQueue(tmp_path, now_ms=lambda: NOW_MS)
        await queue.start()
        try:
            created = await queue.run(
                lambda store: store.create_drawing(_drawing(), drawing_id="drw_q1")
            )
            patched = await queue.run(
                lambda store: store.update_drawing(
                    "drw_q1", DrawingPatch(points=[_point(2_000, "102.00")])
                )
            )
            await queue.run(lambda store: store.soft_delete_drawing("drw_q1"))
            restored = await queue.run(
                lambda store: store.restore_drawing("drw_q1")
            )
            versions = await queue.run(
                lambda store: store.list_drawing_versions("drw_q1")
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

    connection = sqlite3.connect(_db_path(tmp_path))
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
                lambda store: store.create_drawing(_drawing(), drawing_id="drw_q2")
            )
            patches = [
                DrawingPatch(text=f"v{index}")
                for index in range(6)
            ]
            await asyncio.gather(
                *(
                    queue.run(
                        lambda store, patch=patch: store.update_drawing(
                            "drw_q2", patch
                        )
                    )
                    for patch in patches
                )
            )
            latest = await queue.run(lambda store: store.get_drawing("drw_q2"))
            return latest.version
        finally:
            await queue.close()

    assert asyncio.run(scenario()) == 7
