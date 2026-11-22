"""模拟盘按子账户持有跨进程单写锁。"""

import fcntl
import os
from collections.abc import Iterator, Sequence
from contextlib import ExitStack, contextmanager
from pathlib import Path


class AccountOwnershipError(ValueError):
    """已有另一个账户进程持有子账户。"""


@contextmanager
def hold_sub_accounts(data_dir: Path, sub_accounts: Sequence[str]) -> Iterator[None]:
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
        yield
