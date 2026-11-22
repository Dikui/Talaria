"""账户进程单写的 SQLite 执行事件与复式账本。"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from collections.abc import Iterator, Mapping, Sequence
from contextlib import closing, contextmanager
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
def open_ledger(path: Path, run_id: str, sub_accounts: Sequence[str]) -> Iterator[LedgerWriter]:
    """为回测创建独立账本，已有账本通过 read_journal 读取。"""
    claim_writer(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            path.touch(mode=0o600, exist_ok=False)
        except FileExistsError as exc:
            raise LedgerError(f"回测账本已存在：{path}") from exc
        db = open_writer_connection(path)
        try:
            db.execute("PRAGMA foreign_keys = ON")
            db.executescript(_SCHEMA)
            yield LedgerWriter(db, run_id, sub_accounts)
        finally:
            checkpoint_and_close(db)
    finally:
        release_writer(path)


class LedgerWriter:
    def __init__(self, db: sqlite3.Connection, run_id: str, sub_accounts: Sequence[str]) -> None:
        self._db = db
        self._run_id = run_id
        self._event_count = 0
        self._last_ts = -1
        self._balances: dict[str, dict[tuple[str, str], Decimal]] = {
            account: {} for account in sub_accounts
        }

    def balances(self, sub_account_id: str) -> Mapping[tuple[str, str], Decimal]:
        return MappingProxyType(self._balances[sub_account_id].copy())

    def record(self, events: Sequence[ExecutionEvent]) -> None:
        if not events:
            return
        next_balances: dict[str, dict[tuple[str, str], Decimal]] = {}
        last_ts = self._last_ts
        for event in events:
            if event.ts_event < last_ts:
                raise LedgerError("账本事件发生时间不得倒退")
            last_ts = event.ts_event
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

        self._db.execute("BEGIN IMMEDIATE")
        try:
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
            self._db.commit()
        except BaseException:
            self._db.rollback()
            raise
        self._balances.update(next_balances)
        self._event_count += len(events)
        self._last_ts = last_ts
