"""本机交易员登记策略来源并批准明确的代码版本。"""

from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from hashlib import sha256
import os
from pathlib import Path
import sqlite3
from typing import Annotated, Literal

from pydantic import StringConstraints

from talaria.account.scheme import NonEmpty, SchemeError
from talaria.foundation.types import ContractModel

type StrategySource = Literal["human", "agent"]
type ContentHash = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class StrategyApproval(ContractModel):
    approved_by: NonEmpty
    approved_at: datetime
    sha256: ContentHash


class StrategyRegistration(ContractModel):
    path: Path
    source: StrategySource
    sha256: ContentHash
    approval: StrategyApproval | None = None


class StrategyCodeRegistry:
    def __init__(self, data_dir: Path) -> None:
        self._path = data_dir.expanduser().resolve() / "strategy-code.sqlite"

    def register(self, path: Path, source: StrategySource) -> StrategyRegistration:
        path = _strategy_path(path)
        with self._write_transaction() as db:
            registration = StrategyRegistration(
                path=path, source=source, sha256=sha256(path.read_bytes()).hexdigest(),
            )
            previous = self._read(db, path)
            if previous is not None:
                if previous.source != source:
                    raise SchemeError(f"{path}：已登记的策略来源不可变更")
                if previous.sha256 == registration.sha256:
                    return previous
            self._write(db, registration)
        return registration

    def approve(self, path: Path, digest: str, approved_by: str) -> StrategyRegistration:
        path = _strategy_path(path)
        approval = StrategyApproval(approved_by=approved_by, approved_at=datetime.now(UTC), sha256=digest)
        with self._write_transaction() as db:
            registration = self._read(db, path)
            if registration is None or registration.source != "agent":
                raise SchemeError(f"{path}：请先登记为 agent 策略")
            if digest != registration.sha256 or digest != sha256(path.read_bytes()).hexdigest():
                raise SchemeError(f"{path}：批准哈希与当前登记的策略代码不符，请重新登记并审阅")
            registration = registration.model_copy(update={"approval": approval})
            self._write(db, registration)
        return registration

    def paper_source(self, path: Path) -> tuple[StrategySource, bytes]:
        """返回校验过的代码字节，调用者直接运行此版本。"""
        path = _strategy_path(path)
        registration = None
        if self._path.is_file():
            with closing(sqlite3.connect(f"{self._path.as_uri()}?mode=ro", uri=True)) as db:
                registration = self._read(db, path)
        if registration is None:
            raise SchemeError(f"{path}：策略代码未登记")
        source = path.read_bytes()
        digest = sha256(source).hexdigest()
        if registration.sha256 != digest:
            raise SchemeError(f"{path}：策略代码哈希不符，请重新登记")
        if registration.source == "agent":
            if registration.approval is None:
                raise SchemeError(f"{path}：agent 策略未批准")
            if registration.approval.sha256 != digest:
                raise SchemeError(f"{path}：agent 策略批准哈希不符")
        return registration.source, source

    @contextmanager
    def _write_transaction(self) -> Iterator[sqlite3.Connection]:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        os.close(os.open(self._path, os.O_CREAT | os.O_RDONLY, 0o600))
        with closing(sqlite3.connect(self._path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("CREATE TABLE IF NOT EXISTS strategy_code (path TEXT PRIMARY KEY, registration TEXT NOT NULL)")
            yield db

    @staticmethod
    def _read(db: sqlite3.Connection, path: Path) -> StrategyRegistration | None:
        row = db.execute("SELECT registration FROM strategy_code WHERE path = ?", (str(path),)).fetchone()
        return StrategyRegistration.model_validate_json(row[0]) if row else None

    @staticmethod
    def _write(db: sqlite3.Connection, registration: StrategyRegistration) -> None:
        db.execute(
            "INSERT INTO strategy_code (path, registration) VALUES (?, ?) "
            "ON CONFLICT(path) DO UPDATE SET registration=excluded.registration",
            (str(registration.path), registration.model_dump_json()),
        )


def _strategy_path(path: Path) -> Path:
    path = path.expanduser().resolve()
    if path.suffix != ".py":
        raise SchemeError(f"{path}：策略代码须为 Python 文件")
    return path
