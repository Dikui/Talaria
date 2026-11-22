"""只收集普通 Python 源文件，以相对路径和完整字节确定策略快照。"""

from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
import os
from pathlib import Path
import stat
from tempfile import TemporaryDirectory
from zipfile import ZipFile, ZipInfo

from talaria.account.scheme import SchemeError

MAX_SOURCE_BYTES = 1024 * 1024
MAX_SNAPSHOT_BYTES = 8 * MAX_SOURCE_BYTES
MAX_SOURCE_FILES = 256
MAX_DIRECTORY_ENTRIES = 16_384


def _read_source(directory: int, name: str) -> bytes:
    fd = os.open(name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=directory)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise SchemeError(f"{name}：策略代码须为普通文件")
        if info.st_size <= MAX_SOURCE_BYTES:
            data = stream.read(MAX_SOURCE_BYTES + 1)
            if len(data) <= MAX_SOURCE_BYTES:
                return data
        raise SchemeError(f"{name}：策略代码超过大小上限")


@dataclass(frozen=True)
class StrategySnapshot:
    entry: str
    archive: bytes

    @property
    def digest(self) -> str:
        return sha256(self.archive).hexdigest()

    @classmethod
    def capture(cls, path: Path) -> "StrategySnapshot":
        files = {}
        total_bytes = 0
        entries = 0
        directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW

        def collect(directory: int, prefix: Path):
            nonlocal total_bytes, entries
            with os.scandir(directory) as listing:
                for item in listing:
                    entries += 1
                    if entries > MAX_DIRECTORY_ENTRIES or len(prefix.parts) > 32:
                        raise SchemeError("策略目录超过快照遍历上限")
                    relative = prefix / item.name
                    if item.is_dir(follow_symlinks=False):
                        child = os.open(item.name, directory_flags, dir_fd=directory)
                        try:
                            collect(child, relative)
                        finally:
                            os.close(child)
                    elif relative.suffix == ".py":
                        data = _read_source(directory, item.name)
                        total_bytes += len(data)
                        if total_bytes > MAX_SNAPSHOT_BYTES or len(files) >= MAX_SOURCE_FILES:
                            raise SchemeError("策略代码快照超过上限")
                        files[str(relative)] = data

        root = os.open(path.parent, directory_flags)
        try:
            collect(root, Path())
        finally:
            os.close(root)
        if path.name not in files:
            raise SchemeError(f"{path}：找不到策略入口普通文件")
        buffer = BytesIO()
        with ZipFile(buffer, "w") as archive:
            archive.comment = path.name.encode()
            for name, data in sorted(files.items()):
                # ZipInfo 的固定时间戳保证相同内容在不同机器上得到相同哈希。
                archive.writestr(ZipInfo(name), data)
        return cls(path.name, buffer.getvalue())

    @contextmanager
    def materialize(self):
        with TemporaryDirectory(prefix="talaria-strategy-") as temporary:
            root = Path(temporary).resolve()
            with ZipFile(BytesIO(self.archive)) as archive:
                archive.extractall(root)
            for path in root.rglob("*"):
                path.chmod(0o500 if path.is_dir() else 0o400)
            root.chmod(0o500)
            yield root / self.entry
