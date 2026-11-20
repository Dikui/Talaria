"""协同状态库各仓储共用的 SQLite 写事务。"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager


@contextmanager
def write_transaction(connection: sqlite3.Connection) -> Iterator[None]:
    """让一次写操作涉及的多条 SQL 一起提交或回滚。"""

    connection.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        connection.execute("ROLLBACK")
        raise
    else:
        connection.execute("COMMIT")
