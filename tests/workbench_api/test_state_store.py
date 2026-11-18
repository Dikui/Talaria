"""协同状态单写者：WAL、串行提交、关闭刷盘与 checkpoint。

来源：任务单 06、docs/architecture.md §5.2 与 ADR-0006。测试只通过公开的
``StateWriterQueue``/``StateStore`` 边界以及关闭后的 SQLite 文件观察行为。
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

from talaria.foundation.errors import SingleWriterViolationError
from talaria.workbench_api.state_store import StateStore, StateWriterQueue

pytestmark = [pytest.mark.medium, pytest.mark.spec]

NOW_MS = 1_795_022_974_103


def _db_path(data_dir: Path) -> Path:
    return data_dir / "state" / "collaboration_state.sqlite"


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
