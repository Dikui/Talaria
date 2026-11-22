"""账户进程单写的 SQLite 执行事件与复式账本。"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from collections.abc import Iterator, Mapping, Sequence
from contextlib import closing, contextmanager, nullcontext
from dataclasses import dataclass
from decimal import Decimal, localcontext
from fractions import Fraction
from pathlib import Path
from types import MappingProxyType

from talaria.account.contracts import ExecutionEvent, LedgerPosting
from talaria.perception.sqlite import checkpoint_and_close, claim_writer, open_writer_connection, release_writer

_SCHEMA = """
CREATE TABLE execution_events (
    seq INTEGER PRIMARY KEY,
    payload TEXT NOT NULL
);
CREATE TABLE ledger_entries (
    event_seq INTEGER PRIMARY KEY REFERENCES execution_events(seq),
    run_id TEXT,
    sub_account_id TEXT NOT NULL,
    ts_event INTEGER NOT NULL
);
CREATE TABLE ledger_postings (
    event_seq INTEGER NOT NULL REFERENCES ledger_entries(event_seq),
    ordinal INTEGER NOT NULL,
    account TEXT NOT NULL,
    currency TEXT NOT NULL,
    amount TEXT NOT NULL,
    PRIMARY KEY (event_seq, ordinal)
);
"""


class LedgerError(ValueError):
    """执行事件无法作为平衡的复式分录入账。"""


def check_postings(event: ExecutionEvent) -> None:
    totals: dict[str, Fraction] = defaultdict(Fraction)
    for posting in event.postings:
        if not posting.amount:
            raise LedgerError("复式分录金额必须非零")
        totals[posting.currency] += Fraction(posting.amount)
    if any(totals.values()):
        raise LedgerError(f"子账户 {event.sub_account_id} 的复式分录借贷不平")


def _add(left: Decimal, right: Decimal) -> Decimal:
    # 保留最大整数位、最小小数位和可能的进位。
    precision = max(left.adjusted(), right.adjusted(), 0) - min(
        left.as_tuple().exponent, right.as_tuple().exponent, 0,
    ) + 2
    with localcontext(prec=precision):
        return left + right


@dataclass(frozen=True, slots=True)
class LedgerEntry:
    sub_account_id: str
    ts_event: int
    postings: tuple[LedgerPosting, ...]
    event_id: str


@dataclass(frozen=True, slots=True)
class RunJournal:
    events: tuple[ExecutionEvent, ...]
    entries: tuple[LedgerEntry, ...]


def read_journal(path: Path) -> RunJournal:
    """按入账顺序读取，剔除运行 ID 与 SQLite 物理行标识。"""
    with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)) as db:
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        events_by_seq = {}
        for index, (seq, payload) in enumerate(
            db.execute("SELECT seq, payload FROM execution_events ORDER BY seq"), start=1,
        ):
            event = ExecutionEvent.model_validate_json(payload)
            if event.event_id is None:
                event = event.model_copy(update={"event_id": f"event-{index}"})
            events_by_seq[seq] = event
        postings = defaultdict(list)
        for seq, account, currency, amount in db.execute(
            "SELECT event_seq, account, currency, amount FROM ledger_postings ORDER BY event_seq, ordinal",
        ):
            postings[seq].append(LedgerPosting(account=account, currency=currency, amount=amount))
        entries = tuple(
            LedgerEntry(account, ts, tuple(postings[seq]), events_by_seq[seq].event_id)
            for seq, account, ts in db.execute(
                "SELECT event_seq, sub_account_id, ts_event FROM ledger_entries ORDER BY event_seq",
            )
        )
    return RunJournal(tuple(events_by_seq.values()), entries)


@contextmanager
def open_ledger(
    path: Path, run_id: str | None, sub_accounts: Sequence[str], *, resume: bool = False,
) -> Iterator[LedgerWriter]:
    """创建独立账本，模拟盘可以在子账户锁内继续追加。"""
    claim_writer(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = resume and path.is_file()
        if not existing:
            try:
                path.touch(mode=0o600, exist_ok=False)
            except FileExistsError as exc:
                raise LedgerError(f"回测账本已存在：{path}") from exc
        db = open_writer_connection(path)
        try:
            db.execute("PRAGMA foreign_keys = ON")
            initialized = existing and db.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()
            previous = read_journal(path) if initialized else RunJournal((), ())
            if not initialized:
                db.executescript(f"BEGIN IMMEDIATE;{_SCHEMA}COMMIT;")
            if resume:
                db.execute("""CREATE TABLE IF NOT EXISTS paper_checkpoint (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1), event_count INTEGER NOT NULL, payload TEXT NOT NULL
                )""")
            yield LedgerWriter(db, run_id, sub_accounts, previous)
        finally:
            checkpoint_and_close(db)
    finally:
        release_writer(path)


class LedgerWriter:
    def __init__(
        self, db: sqlite3.Connection, run_id: str | None, sub_accounts: Sequence[str], previous: RunJournal,
    ) -> None:
        self._db = db
        self._run_id = run_id
        postings = {entry.event_id: entry.postings for entry in previous.entries}
        self.previous = tuple(event.model_copy(update={"postings": postings[event.event_id]})
                              if event.event_id in postings else event for event in previous.events)
        self._event_count = len(previous.events)
        self._last_ts = max((event.ts_event for event in previous.events), default=-1)
        self._balances: dict[str, dict[tuple[str, str], Decimal]] = {
            account: {} for account in sub_accounts
        }
        for entry in previous.entries:
            balances = self._balances[entry.sub_account_id]
            for posting in entry.postings:
                key = (posting.currency, posting.account)
                balances[key] = _add(balances.get(key, Decimal(0)), posting.amount)

    @contextmanager
    def transaction(self):
        balances, count, last_ts = self._balances.copy(), self._event_count, self._last_ts
        self._db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self._db.commit()
        except BaseException:
            self._db.rollback()
            self._balances, self._event_count, self._last_ts = balances, count, last_ts
            raise

    def save_checkpoint(self, payload: dict) -> None:
        self._db.execute("INSERT OR REPLACE INTO paper_checkpoint VALUES (1, ?, ?)",
                         (self._event_count, json.dumps(payload, separators=(",", ":"))))

    def checkpoint(self) -> dict | None:
        row = self._db.execute("SELECT event_count, payload FROM paper_checkpoint WHERE singleton = 1").fetchone()
        if row is None:
            return None
        if row[0] != self._event_count:
            raise LedgerError("模拟盘检查点与账本事件序号不一致")
        return json.loads(row[1])

    def balances(self, sub_account_id: str) -> Mapping[tuple[str, str], Decimal]:
        return MappingProxyType(self._balances[sub_account_id].copy())

    def record(self, events: Sequence[ExecutionEvent], *, allow_late_events: bool = False) -> None:
        if not events:
            return
        next_balances: dict[str, dict[tuple[str, str], Decimal]] = {}
        last_ts = self._last_ts
        for event in events:
            if not allow_late_events and event.ts_event < last_ts:
                raise LedgerError("账本事件发生时间不得倒退")
            last_ts = max(last_ts, event.ts_event)
            if event.sub_account_id not in self._balances:
                raise LedgerError(f"账本不承载子账户 {event.sub_account_id}")
            if not event.postings:
                continue
            if event.sub_account_id not in next_balances:
                next_balances[event.sub_account_id] = self._balances[event.sub_account_id].copy()
            balances = next_balances[event.sub_account_id]
            check_postings(event)
            for posting in event.postings:
                key = (posting.currency, posting.account)
                balances[key] = _add(balances.get(key, Decimal(0)), posting.amount)

        with nullcontext() if self._db.in_transaction else self.transaction():
            for index, event in enumerate(events, start=self._event_count + 1):
                event = event.model_copy(update={"event_id": f"event-{index}"})
                seq = self._db.execute(
                    "INSERT INTO execution_events (payload) VALUES (?)",
                    (event.model_dump_json(exclude={"postings"}),),
                ).lastrowid
                if event.postings:
                    self._db.execute(
                        "INSERT INTO ledger_entries VALUES (?, ?, ?, ?)",
                        (seq, self._run_id, event.sub_account_id, event.ts_event),
                    )
                    self._db.executemany(
                        "INSERT INTO ledger_postings VALUES (?, ?, ?, ?, ?)",
                        (
                            (seq, i, posting.account, posting.currency, format(posting.amount, "f"))
                            for i, posting in enumerate(event.postings)
                        ),
                    )
            self._balances.update(next_balances)
            self._event_count += len(events)
            self._last_ts = last_ts
