"""从模拟盘账本投影持仓、回合和成交。"""

import re
from collections import defaultdict
from collections.abc import Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager
from decimal import Decimal
from pathlib import Path

from talaria.account.contracts import ExecutionEvent
from talaria.account.ledger import LedgerError, LedgerWriter, open_ledger, read_journal
from talaria.account.ownership import AccountOwnership
from talaria.foundation.paper import ACCOUNT_ID_PATTERN, PaperAccountView, PaperFill, PaperRound


def _ledger_path(data_dir: Path, account: str) -> Path | None:
    root = (data_dir / "accounts").resolve()
    path = root / account / "ledger.sqlite"
    if re.fullmatch(ACCOUNT_ID_PATTERN, account) and path.is_file() and path.resolve() == path:
        return path
    return None


def list_accounts(data_dir: Path) -> list[str]:
    return sorted(
        path.parent.name for path in (data_dir / "accounts").glob("*/ledger.sqlite")
        if _ledger_path(data_dir, path.parent.name) is not None
    )


def account_view(data_dir: Path, account: str) -> PaperAccountView | None:
    path = _ledger_path(data_dir, account)
    if path is None:
        return None
    rounds: dict[str, PaperRound] = {}
    fills = []
    for event in read_journal(path).events:
        position = event.position
        if position is None or event.sub_account_id != account:
            continue
        previous = rounds.get(position.round_id)
        targets = event.take_profit or (previous.take_profit if previous else ())
        rounds[position.round_id] = PaperRound(**position.model_dump(), take_profit=targets)
        if event.kind in ("ENTRY_FILLED", "EXIT_FILLED") and event.price is not None and event.quantity is not None:
            fills.append(PaperFill(
                event_id=event.event_id, kind=event.kind, ts_event=event.ts_event, round_id=position.round_id,
                instrument_id=position.instrument_id, quantity=event.quantity, price=event.price,
            ))
    ordered = list(reversed(rounds.values()))
    return PaperAccountView(
        sub_account_id=account, positions=[row for row in ordered if row.quantity > 0],
        rounds=ordered, fills=list(reversed(fills)),
    )


class PaperLedger:
    def __init__(self, ledgers: Mapping[str, LedgerWriter], ownership: AccountOwnership) -> None:
        self._ledgers = ledgers
        self._ownership = ownership
        self.previous = tuple(event for ledger in ledgers.values() for event in ledger.previous)

    def balances(self, sub_account_id: str) -> Mapping[tuple[str, str], Decimal]:
        return self._ledgers[sub_account_id].balances(sub_account_id)

    def record(self, events: Sequence[ExecutionEvent]) -> None:
        grouped = defaultdict(list)
        if any(event.sub_account_id not in self._ledgers for event in events):
            raise LedgerError("账本不承载事件所属子账户")
        for event in events:
            grouped[event.sub_account_id].append(event)
        with self._ownership.guard():
            for account, rows in grouped.items():
                self._ledgers[account].record(rows, allow_late_events=True)


@contextmanager
def open_paper_ledger(
    data_dir: Path, accounts: Sequence[str], ownership: AccountOwnership,
) -> Iterator[PaperLedger]:
    with ownership.guard(), ExitStack() as stack:
        for account in accounts:
            view = account_view(data_dir, account)
            if view is not None and (view.positions or any(row.phase == "PENDING_ENTRY" for row in view.rounds)):
                raise LedgerError(f"子账户 {account} 有未结束的回合，无法安全恢复模拟盘")
        ledgers = {
            account: stack.enter_context(open_ledger(
                data_dir / "accounts" / account / "ledger.sqlite", None, (account,), resume=True,
            ))
            for account in accounts
        }
        yield PaperLedger(ledgers, ownership)
