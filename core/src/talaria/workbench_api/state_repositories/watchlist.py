"""自选观察池分组及条目仓储。"""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Callable
from dataclasses import dataclass

from talaria.workbench_api.state_repositories._sqlite import write_transaction

SCHEMA = """
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


#: 系统动态分组排在手工分组之后；前端展示时会重新编号为连续 position。
SYSTEM_GROUP_POSITION = 1_000_000
SCANNER_FOCUS_GROUP_ID = "scanner-focus"


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


class WatchlistRepository:
    def __init__(
        self, connection: sqlite3.Connection, now_ms: Callable[[], int]
    ) -> None:
        self._connection = connection
        self._now_ms = now_ms

    def list(self) -> WatchlistRecords:
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

    def get_group(self, group_id: str) -> WatchlistGroupRecord | None:
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

    def create_group(
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
        with write_transaction(self._connection):
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

    def rename_group(self, group_id: str, name: str) -> None:
        clean_name = name.strip()
        if not clean_name:
            raise ValueError("分组名称不能为空")
        cursor = self._connection.execute(
            "UPDATE watchlist_groups SET name = ?, updated_ts = ? WHERE group_id = ?",
            (clean_name, self._now_ms(), group_id),
        )
        if cursor.rowcount == 0:
            raise KeyError(group_id)

    def delete_group(self, group_id: str) -> None:
        with write_transaction(self._connection):
            cursor = self._connection.execute(
                "DELETE FROM watchlist_groups WHERE group_id = ?", (group_id,)
            )
            if cursor.rowcount == 0:
                raise KeyError(group_id)
            self._connection.execute(
                "DELETE FROM watchlist_items WHERE group_id = ?", (group_id,)
            )

    def add_item(self, group_id: str, instrument_id: str) -> None:
        now = self._now_ms()
        with write_transaction(self._connection):
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

    def remove_item(self, group_id: str, instrument_id: str) -> None:
        cursor = self._connection.execute(
            "DELETE FROM watchlist_items WHERE group_id = ? AND instrument_id = ?",
            (group_id, instrument_id),
        )
        if cursor.rowcount == 0:
            raise KeyError(instrument_id)

    def reorder_groups(self, ids: list[str]) -> None:
        """只调整手工分组顺序；系统分组位置保持不变。"""
        now = self._now_ms()
        with write_transaction(self._connection):
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

    def reorder_items(self, group_id: str, ids: list[str]) -> None:
        now = self._now_ms()
        with write_transaction(self._connection):
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
        with write_transaction(self._connection):
            existing = self.get_group(group_id)
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
