"""模拟盘按子账户持有跨进程单写锁。"""

import fcntl
import os
from collections.abc import Iterator, Sequence
from contextlib import ExitStack, contextmanager
from pathlib import Path
from threading import RLock


class AccountOwnershipError(ValueError):
    """已有另一个账户进程持有子账户。"""


class AccountOwnership:
    def __init__(self) -> None:
        self._lock = RLock()
        self._pid = os.getpid()
        self._active = True

    @contextmanager
    def guard(self) -> Iterator[None]:
        if self._pid != os.getpid():
            raise AccountOwnershipError("账户进程未持有子账户 owner.lock")
        with self._lock:
            if not self._active:
                raise AccountOwnershipError("账户进程已失去子账户 owner.lock")
            yield

    def _release(self) -> None:
        with self._lock:
            self._active = False


@contextmanager
def hold_sub_accounts(data_dir: Path, sub_accounts: Sequence[str]) -> Iterator[AccountOwnership]:
    with ExitStack() as owners:
        for sub_account_id in sorted(sub_accounts):
            path = data_dir / "accounts" / sub_account_id / "owner.lock"
            path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
            # 关闭会释放 flock。保留文件，避免另一个进程锁住不同 inode。
            owners.callback(os.close, fd)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise AccountOwnershipError(f"子账户 {sub_account_id} 已被另一账户进程占用（{path}）") from None
        ownership = AccountOwnership()
        try:
            yield ownership
        finally:
            ownership._release()
