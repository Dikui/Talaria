"""协同与感知状态的唯一写者：单线程 SQLite + 单 asyncio 协程串行队列。

架构约定（docs/architecture.md §5.2、ADR-0006）：``state/collaboration_state.sqlite``
只允许 daemon 内一个写者；写操作排队到一个协程，逐条提交，退出时刷盘并
``PRAGMA wal_checkpoint(TRUNCATE)``。05 的 watchlist / insight / staged plan
可直接复用 ``events`` 与 ``meta`` 表。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar

from talaria.foundation.sqlite import (
    checkpoint_and_close,
    claim_writer,
    release_writer,
)

STATE_SUBDIR = Path("state")
STATE_DB = STATE_SUBDIR / "collaboration_state.sqlite"

#: 系统动态分组排在手工分组之后；前端展示时会重新编号为连续 position。
SYSTEM_GROUP_POSITION = 1_000_000
SCANNER_FOCUS_GROUP_ID = "scanner-focus"

T = TypeVar("T")


@dataclass(frozen=True)
class WatchlistGroupRecord:
    group_id: str
    name: str
    position: int
    kind: str
    is_system: bool


@dataclass(frozen=True)
class WatchlistItemRecord:
    group_id: str
    instrument_id: str
    position: int


@dataclass(frozen=True)
class WatchlistRecords:
    groups: tuple[WatchlistGroupRecord, ...]
    items: tuple[WatchlistItemRecord, ...]

    def items_for(self, group_id: str) -> tuple[WatchlistItemRecord, ...]:
        return tuple(item for item in self.items if item.group_id == group_id)


def _now_ms() -> int:
    return int(time.time() * 1000)


class StateStore:
    """唯一写者持有的 SQLite 连接；所有方法必须在同一线程/协程内串行调用。"""

    def __init__(self, data_dir: Path | str, *, now_ms: Callable[[], int] = _now_ms) -> None:
        self._db_path = Path(data_dir) / STATE_DB
        self._now_ms = now_ms
        self._closed = False
        claim_writer(self._db_path)
        try:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            self._connection = sqlite3.connect(
                self._db_path, isolation_level=None, check_same_thread=False
            )
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA synchronous=FULL")
            self._connection.execute("PRAGMA busy_timeout=5000")
            self._connection.execute("PRAGMA foreign_keys=ON")
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_ts INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    ts INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS watchlist_groups (
                    group_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    position INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    is_system INTEGER NOT NULL CHECK (is_system IN (0, 1)),
                    created_ts INTEGER NOT NULL,
                    updated_ts INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS watchlist_items (
                    group_id TEXT NOT NULL,
                    instrument_id TEXT NOT NULL,
                    position INTEGER NOT NULL,
                    created_ts INTEGER NOT NULL,
                    updated_ts INTEGER NOT NULL,
                    PRIMARY KEY (group_id, instrument_id),
                    FOREIGN KEY (group_id) REFERENCES watchlist_groups(group_id)
                        ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS watchlist_items_group_idx
                    ON watchlist_items(group_id, position);
                """
            )
        except BaseException:
            release_writer(self._db_path)
            raise

    def append_event(
        self,
        kind: str,
        payload: Mapping[str, Any],
        *,
        ts: int | None = None,
    ) -> int:
        cursor = self._connection.execute(
            "INSERT INTO events (kind, payload, ts) VALUES (?, ?, ?)",
            (
                kind,
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
                self._now_ms() if ts is None else ts,
            ),
        )
        return int(cursor.lastrowid or 0)

    def set_meta(self, key: str, value: Any, *, ts: int | None = None) -> None:
        self._connection.execute(
            """
            INSERT INTO meta (key, value, updated_ts) VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value, updated_ts = excluded.updated_ts
            """,
            (
                key,
                json.dumps(value, ensure_ascii=False, sort_keys=True),
                self._now_ms() if ts is None else ts,
            ),
        )

    def get_meta(self, key: str) -> Any | None:
        row = self._connection.execute(
            "SELECT value FROM meta WHERE key = ?", (key,)
        ).fetchone()
        return None if row is None else json.loads(row[0])

    # ------------------------------------------------------------- Watchlist

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        """显式事务；单写者下仍保证多语句更新的原子性。"""
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self._connection.execute("ROLLBACK")
            raise
        else:
            self._connection.execute("COMMIT")

    def list_watchlist(self) -> WatchlistRecords:
        groups = tuple(
            WatchlistGroupRecord(
                group_id=row[0],
                name=row[1],
                position=int(row[2]),
                kind=row[3],
                is_system=bool(row[4]),
            )
            for row in self._connection.execute(
                """
                SELECT group_id, name, position, kind, is_system
                FROM watchlist_groups
                ORDER BY is_system, position, group_id
                """
            )
        )
        items = tuple(
            WatchlistItemRecord(
                group_id=row[0],
                instrument_id=row[1],
                position=int(row[2]),
            )
            for row in self._connection.execute(
                """
                SELECT group_id, instrument_id, position
                FROM watchlist_items
                ORDER BY group_id, position, instrument_id
                """
            )
        )
        return WatchlistRecords(groups=groups, items=items)

    def get_watchlist_group(self, group_id: str) -> WatchlistGroupRecord | None:
        row = self._connection.execute(
            """
            SELECT group_id, name, position, kind, is_system
            FROM watchlist_groups WHERE group_id = ?
            """,
            (group_id,),
        ).fetchone()
        if row is None:
            return None
        return WatchlistGroupRecord(
            group_id=row[0],
            name=row[1],
            position=int(row[2]),
            kind=row[3],
            is_system=bool(row[4]),
        )

    def create_watchlist_group(
        self,
        name: str,
        *,
        group_id: str | None = None,
    ) -> str:
        clean_name = name.strip()
        if not clean_name:
            raise ValueError("分组名称不能为空")
        identifier = group_id or uuid.uuid4().hex
        now = self._now_ms()
        with self._transaction():
            row = self._connection.execute(
                "SELECT COALESCE(MAX(position) + 1, 0) FROM watchlist_groups "
                "WHERE is_system = 0"
            ).fetchone()
            position = int(row[0]) if row is not None else 0
            self._connection.execute(
                """
                INSERT INTO watchlist_groups
                    (group_id, name, position, kind, is_system, created_ts, updated_ts)
                VALUES (?, ?, ?, 'MANUAL', 0, ?, ?)
                """,
                (identifier, clean_name, position, now, now),
            )
        return identifier

    def rename_watchlist_group(self, group_id: str, name: str) -> None:
        clean_name = name.strip()
        if not clean_name:
            raise ValueError("分组名称不能为空")
        cursor = self._connection.execute(
            "UPDATE watchlist_groups SET name = ?, updated_ts = ? WHERE group_id = ?",
            (clean_name, self._now_ms(), group_id),
        )
        if cursor.rowcount == 0:
            raise KeyError(group_id)

    def delete_watchlist_group(self, group_id: str) -> None:
        with self._transaction():
            cursor = self._connection.execute(
                "DELETE FROM watchlist_groups WHERE group_id = ?", (group_id,)
            )
            if cursor.rowcount == 0:
                raise KeyError(group_id)
            self._connection.execute(
                "DELETE FROM watchlist_items WHERE group_id = ?", (group_id,)
            )

    def add_watchlist_item(self, group_id: str, instrument_id: str) -> None:
        now = self._now_ms()
        with self._transaction():
            row = self._connection.execute(
                "SELECT COALESCE(MAX(position) + 1, 0) FROM watchlist_items "
                "WHERE group_id = ?",
                (group_id,),
            ).fetchone()
            position = int(row[0]) if row is not None else 0
            try:
                self._connection.execute(
                    """
                    INSERT INTO watchlist_items
                        (group_id, instrument_id, position, created_ts, updated_ts)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (group_id, instrument_id, position, now, now),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError("标的已在分组中") from exc

    def remove_watchlist_item(self, group_id: str, instrument_id: str) -> None:
        cursor = self._connection.execute(
            "DELETE FROM watchlist_items WHERE group_id = ? AND instrument_id = ?",
            (group_id, instrument_id),
        )
        if cursor.rowcount == 0:
            raise KeyError(instrument_id)

    def reorder_watchlist_groups(self, ids: list[str]) -> None:
        """只调整手工分组顺序；系统分组位置保持不变。"""
        now = self._now_ms()
        with self._transaction():
            for position, group_id in enumerate(ids):
                cursor = self._connection.execute(
                    """
                    UPDATE watchlist_groups
                    SET position = ?, updated_ts = ?
                    WHERE group_id = ? AND is_system = 0
                    """,
                    (position, now, group_id),
                )
                if cursor.rowcount == 0:
                    raise KeyError(group_id)

    def reorder_watchlist_items(self, group_id: str, ids: list[str]) -> None:
        now = self._now_ms()
        with self._transaction():
            for position, instrument_id in enumerate(ids):
                cursor = self._connection.execute(
                    """
                    UPDATE watchlist_items
                    SET position = ?, updated_ts = ?
                    WHERE group_id = ? AND instrument_id = ?
                    """,
                    (position, now, group_id, instrument_id),
                )
                if cursor.rowcount == 0:
                    raise KeyError(instrument_id)

    def upsert_system_group(
        self,
        name: str,
        instrument_ids: list[str],
        *,
        group_id: str = SCANNER_FOCUS_GROUP_ID,
    ) -> None:
        """供扫描器（任务单 15）写入动态分组；本单只读展示。"""
        clean_name = name.strip()
        if not clean_name:
            raise ValueError("分组名称不能为空")
        now = self._now_ms()
        wanted = list(dict.fromkeys(instrument_ids))
        with self._transaction():
            existing = self.get_watchlist_group(group_id)
            if existing is None:
                row = self._connection.execute(
                    "SELECT COUNT(*) FROM watchlist_groups WHERE is_system = 1"
                ).fetchone()
                position = SYSTEM_GROUP_POSITION + (int(row[0]) if row is not None else 0)
                self._connection.execute(
                    """
                    INSERT INTO watchlist_groups
                        (group_id, name, position, kind, is_system, created_ts, updated_ts)
                    VALUES (?, ?, ?, 'SCANNER_FOCUS', 1, ?, ?)
                    """,
                    (group_id, clean_name, position, now, now),
                )
            else:
                self._connection.execute(
                    """
                    UPDATE watchlist_groups
                    SET name = ?, kind = 'SCANNER_FOCUS', is_system = 1, updated_ts = ?
                    WHERE group_id = ?
                    """,
                    (clean_name, now, group_id),
                )
            if wanted:
                placeholders = ",".join("?" for _ in wanted)
                self._connection.execute(
                    f"DELETE FROM watchlist_items WHERE group_id = ? "
                    f"AND instrument_id NOT IN ({placeholders})",
                    (group_id, *wanted),
                )
            else:
                self._connection.execute(
                    "DELETE FROM watchlist_items WHERE group_id = ?", (group_id,)
                )
            for position, instrument_id in enumerate(wanted):
                self._connection.execute(
                    """
                    INSERT INTO watchlist_items
                        (group_id, instrument_id, position, created_ts, updated_ts)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(group_id, instrument_id) DO UPDATE SET
                        position = excluded.position, updated_ts = excluded.updated_ts
                    """,
                    (group_id, instrument_id, position, now, now),
                )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            checkpoint_and_close(self._connection)
        finally:
            release_writer(self._db_path)


class _Request:
    __slots__ = ("operation", "future")

    def __init__(
        self,
        operation: Callable[[StateStore], Any],
        future: asyncio.Future[Any],
    ) -> None:
        self.operation = operation
        self.future = future


class StateWriterQueue:
    """把状态写操作汇聚到单协程；调用者等待对应 future，保证提交顺序与失败可见。"""

    def __init__(
        self,
        data_dir: Path | str,
        *,
        now_ms: Callable[[], int] = _now_ms,
    ) -> None:
        self._store = StateStore(data_dir, now_ms=now_ms)
        self._queue: asyncio.Queue[_Request | object] = asyncio.Queue()
        self._sentinel = object()
        self._task: asyncio.Task[None] | None = None
        self._closed = False

    async def start(self) -> None:
        if self._closed:
            raise RuntimeError("StateWriterQueue 已关闭")
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    async def _submit(self, operation: Callable[[StateStore], Any]) -> Any:
        if self._closed:
            raise RuntimeError("StateWriterQueue 已关闭")
        if self._task is None:
            raise RuntimeError("StateWriterQueue 尚未启动")
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._queue.put_nowait(_Request(operation, future))
        return await future

    async def submit(
        self,
        kind: str,
        payload: Mapping[str, Any],
        *,
        ts: int | None = None,
    ) -> int:
        return await self._submit(
            lambda store: store.append_event(kind, payload, ts=ts)
        )

    async def set_meta(self, key: str, value: Any, *, ts: int | None = None) -> None:
        await self._submit(lambda store: store.set_meta(key, value, ts=ts))

    async def get_meta(self, key: str) -> Any | None:
        return await self._submit(lambda store: store.get_meta(key))

    async def run(self, operation: Callable[[StateStore], T]) -> T:
        """提交任意同步状态操作；读操作也经同一队列，避免读到半提交视图。"""
        return await self._submit(operation)

    async def _run(self) -> None:
        try:
            while True:
                request = await self._queue.get()
                try:
                    if request is self._sentinel:
                        return
                    assert isinstance(request, _Request)
                    try:
                        result = request.operation(self._store)
                    except Exception as error:  # 交给调用者
                        if not request.future.cancelled():
                            request.future.set_exception(error)
                    else:
                        if not request.future.cancelled():
                            request.future.set_result(result)
                finally:
                    self._queue.task_done()
        finally:
            self._store.close()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._task is None:
            self._store.close()
            return
        await self._queue.join()
        self._queue.put_nowait(self._sentinel)
        await self._task

    async def __aenter__(self) -> StateWriterQueue:
        await self.start()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()
