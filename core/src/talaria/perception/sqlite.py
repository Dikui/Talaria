"""本地 SQLite 单写者守卫与 WAL 生命周期辅助。

架构按“进程内唯一独占写者”拆分数据库（ADR-0006）。本模块只提供最小的
同进程互斥：同一路径的第二个写者在打开时立即失败，而不是排队等待或并发写。
跨进程的并发仍由 SQLite 的 WAL 锁串行化；写事务一律短小。
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from talaria.foundation.errors import SingleWriterViolationError

WORKBENCH_STATE_DB = Path("state") / "workbench_state.sqlite"
COLLABORATION_STATE_DB = Path("state") / "collaboration_state.sqlite"

_CLAIMS: set[Path] = set()
_CLAIMS_LOCK = threading.Lock()


def claim_writer(db_path: Path | str) -> None:
    """登记一个写者；同一进程重复登记抛 ``SingleWriterViolationError``。"""

    path = Path(db_path).resolve()
    with _CLAIMS_LOCK:
        if path in _CLAIMS:
            raise SingleWriterViolationError(f"同一进程已有写者打开：{path}")
        _CLAIMS.add(path)


def release_writer(db_path: Path | str) -> None:
    """释放写者登记；未登记时无操作，保证关闭路径幂等。"""

    path = Path(db_path).resolve()
    with _CLAIMS_LOCK:
        _CLAIMS.discard(path)


def open_writer_connection(db_path: Path | str) -> sqlite3.Connection:
    """打开单写者连接：WAL、``synchronous=FULL``、显式事务、短 busy 等待。"""

    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(
        path,
        check_same_thread=False,
        isolation_level=None,
        timeout=5.0,
    )
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA busy_timeout=5000")
    except BaseException:
        connection.close()
        raise
    return connection


def checkpoint_and_close(connection: sqlite3.Connection) -> None:
    """截断 WAL 后关闭连接；关闭路径的异常不应吞掉，但必须释放句柄。"""

    try:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        connection.close()
