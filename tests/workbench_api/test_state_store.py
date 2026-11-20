"""协同状态单写者：WAL、串行提交、关闭刷盘与 checkpoint。

来源：任务单 06、docs/architecture.md §5.2 与 ADR-0006。测试只通过公开的
``StateWriterQueue``/``StateStore`` 边界以及关闭后的 SQLite 文件观察行为。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from decimal import Decimal
from pathlib import Path
from threading import Event, get_ident

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


# 任务单 02：用可控阻塞代替写入时的慢 fsync。
def test_event_loop_advances_while_a_write_is_blocked(tmp_path: Path) -> None:
    async def scenario() -> None:
        loop = asyncio.get_running_loop()
        writing = asyncio.Event()
        release_write = Event()

        def slow_write(store: StateStore) -> int:
            loop.call_soon_threadsafe(writing.set)
            assert release_write.wait(timeout=2), "事件循环未能在写入期间推进"
            return store.append_event("note", {})

        async with StateWriterQueue(tmp_path) as queue:
            write = asyncio.create_task(queue.run(slow_write))
            try:
                await asyncio.wait_for(writing.wait(), timeout=2)
                await asyncio.sleep(0)
            finally:
                release_write.set()
                result = await write
            assert result == 1

    asyncio.run(scenario())


# 任务单 02：读请求直接读取 WAL 中已提交的数据。
def test_read_sees_committed_value_while_write_is_blocked(tmp_path: Path) -> None:
    async def scenario() -> None:
        loop = asyncio.get_running_loop()
        writing = asyncio.Event()
        release_write = Event()

        def slow_write(store: StateStore) -> None:
            loop.call_soon_threadsafe(writing.set)
            assert release_write.wait(timeout=5)
            store.set_meta("layout", "new")

        async with StateWriterQueue(tmp_path) as queue:
            await queue.set_meta("layout", "old")
            write = asyncio.create_task(queue.run(slow_write))
            try:
                await asyncio.wait_for(writing.wait(), timeout=2)
                assert await asyncio.wait_for(queue.get_meta("layout"), timeout=2) == "old"
            finally:
                release_write.set()
                await write
            assert await queue.get_meta("layout") == "new"

    asyncio.run(scenario())


# 任务单 02：误将写操作放入读入口时，SQLite 必须拒绝写入。
def test_read_connection_rejects_writes(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with StateWriterQueue(tmp_path) as queue:
            with pytest.raises(sqlite3.OperationalError, match="readonly"):
                await queue.read(lambda store: store.set_meta("layout", "new"))
            assert await queue.get_meta("layout") is None

    asyncio.run(scenario())


# 任务单 02：单写线程按提交顺序持久化，失败只传给对应调用者。
@pytest.mark.parametrize("error_type", [ValueError, asyncio.CancelledError])
def test_concurrent_events_preserve_order_after_a_write_fails(
    tmp_path: Path, error_type: type[BaseException]
) -> None:
    async def scenario() -> None:
        loop_thread = get_ident()
        writer_threads: set[int] = set()

        def write(store: StateStore, index: int) -> int:
            writer_threads.add(get_ident())
            if index == 2:
                raise error_type("写入失败")
            return store.append_event("watchlist.added", {"symbol": f"S{index}"})

        async with StateWriterQueue(tmp_path, now_ms=lambda: NOW_MS) as queue:
            results = await asyncio.wait_for(
                asyncio.gather(
                    *(queue.run(lambda store, i=i: write(store, i)) for i in range(6)),
                    return_exceptions=True,
                ),
                timeout=2,
            )
        assert len(writer_threads) == 1 and loop_thread not in writer_threads
        assert results[:2] == [1, 2]
        assert isinstance(results[2], error_type)
        assert results[3:] == [3, 4, 5]

    asyncio.run(scenario())

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
    assert [json.loads(row[2])["symbol"] for row in rows] == ["S0", "S1", "S3", "S4", "S5"]


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

        async def close_and_check() -> None:
            await queue.close()
            assert all(task.done() for task in tasks)

        await asyncio.gather(close_and_check(), close_and_check())

    asyncio.run(scenario())

    wal = Path(f"{_db_path(tmp_path)}-wal")
    assert not wal.exists() or wal.stat().st_size == 0
    StateStore(tmp_path).close()  # 关闭已释放单写者登记。
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
        with pytest.raises(RuntimeError, match="StateWriterQueue 已关闭"):
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
