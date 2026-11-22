"""常驻服务与本机客户端共用的数据目录排他锁。"""

from __future__ import annotations

import errno
import fcntl
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from talaria.foundation.errors import ServiceLockError

ROLE_SERVICE = "service"
ROLE_USAGE_SCAN = "usage-scan"

LOCK_SUBDIR = Path("runtime")
LOCK_NAME = "service.lock"


@dataclass(frozen=True, slots=True)
class LockHolder:
    pid: int | None = None
    role: str | None = None
    started_at: str | None = None


def lock_path(data_dir: str | Path) -> Path:
    return Path(data_dir).resolve() / LOCK_SUBDIR / LOCK_NAME


class ServiceLock:
    """持有 ``<data-dir>/runtime/service.lock`` 的 ``flock`` 排他锁。

    锁文件本身不删除：删除会让后来者锁住新的 inode，而与仍持旧 inode
    锁的进程同时写同一份数据。释放时只清空元数据并 ``LOCK_UN``。
    """

    def __init__(self, data_dir: str | Path, role: str = ROLE_SERVICE) -> None:
        self._data_dir = Path(data_dir).resolve()
        self._role = role
        self.path = lock_path(self._data_dir)
        self._fd: int | None = None
        self._holder: LockHolder | None = None

    @property
    def pid(self) -> int | None:
        return None if self._holder is None else self._holder.pid

    @property
    def role(self) -> str:
        return self._role

    @property
    def holder(self) -> LockHolder | None:
        return self._holder

    def acquire(self) -> LockHolder:
        if self._fd is not None:
            assert self._holder is not None
            return self._holder

        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        except OSError as exc:
            raise ServiceLockError(
                f"无法创建数据目录锁 {self.path}：{exc.strerror or exc}"
            ) from exc

        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK):
                os.close(fd)
                raise ServiceLockError(
                    f"无法锁定 {self.path}：{exc.strerror or exc}"
                ) from exc
            holder = _read_holder(self.path)
            os.close(fd)
            raise ServiceLockError(
                f"数据目录已被另一个 Talaria 进程占用：{self._data_dir}\n"
                f"锁文件：{self.path}\n"
                f"持有者：{_describe_holder(holder)}\n"
                "同一数据目录只允许一个写者；请先停止该进程后重试。"
            ) from None

        holder = LockHolder(
            pid=os.getpid(),
            role=self._role,
            started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
        try:
            _write_holder(fd, self._data_dir, holder)
            os.fchmod(fd, 0o600)
        except BaseException:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
            raise

        self._fd = fd
        self._holder = holder
        return holder

    def release(self) -> None:
        fd = self._fd
        if fd is None:
            return
        self._fd = None
        self._holder = None
        try:
            os.ftruncate(fd, 0)
        except OSError:
            pass
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def __enter__(self) -> ServiceLock:
        self.acquire()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.release()


def probe_lock(data_dir: str | Path) -> LockHolder | None:
    """只探测不占有：返回当前持有者；无人持有时返回 ``None``。"""

    path = lock_path(data_dir)
    if not path.exists():
        return None
    try:
        fd = os.open(path, os.O_RDWR)
    except OSError:
        return _read_holder(path)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK):
                return _read_holder(path)
            return None
        fcntl.flock(fd, fcntl.LOCK_UN)
        return None
    finally:
        os.close(fd)


def _describe_holder(holder: LockHolder) -> str:
    if holder.pid is None and holder.role is None:
        return "未知（锁文件缺少可读的持有者信息）"
    parts = [f"pid={holder.pid}"]
    if holder.role is not None:
        parts.append(f"角色={holder.role}")
    if holder.started_at is not None:
        parts.append(f"启动于 {holder.started_at}")
    return "，".join(parts)


def _read_holder(path: Path) -> LockHolder:
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return LockHolder()
    if not raw:
        return LockHolder()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return LockHolder()
    if not isinstance(payload, dict):
        return LockHolder()
    pid = payload.get("pid")
    role = payload.get("role")
    started_at = payload.get("started_at")
    return LockHolder(
        pid=pid if isinstance(pid, int) and not isinstance(pid, bool) else None,
        role=role if isinstance(role, str) else None,
        started_at=started_at if isinstance(started_at, str) else None,
    )


def _write_holder(fd: int, data_dir: Path, holder: LockHolder) -> None:
    payload = {
        "pid": holder.pid,
        "role": holder.role,
        "data_dir": str(data_dir),
        "started_at": holder.started_at,
    }
    encoded = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
    os.ftruncate(fd, 0)
    os.lseek(fd, 0, os.SEEK_SET)
    os.write(fd, encoded)
    os.fsync(fd)
