"""trading/01 的复式分录在每种币种内精确平衡并按批原子入账。"""

from decimal import Decimal, localcontext

import pytest

from talaria.account.contracts import ExecutionEvent, LedgerPosting
from talaria.account.ledger import LedgerError, open_ledger, read_journal

pytestmark = [pytest.mark.small, pytest.mark.spec]


def postings_event(*postings, ts=1):
    return ExecutionEvent(
        kind="FUNDING_SETTLED", sub_account_id="example", ts_event=ts,
        postings=tuple(
            LedgerPosting(account=account, currency=currency, amount=amount)
            for account, currency, amount in postings
        ),
    )


@pytest.mark.parametrize(("credit_currency", "credit"), [
    ("BTC", "-1000.00000001"),
    ("USDT", "-1000"),
])
def test_unbalanced_currency_aborts_the_entire_step(tmp_path, credit_currency, credit):
    ledger_path = tmp_path / "ledger.sqlite"
    debit = "1000.00000001"
    balanced = postings_event(("CASH_AVAILABLE", "USDT", debit), ("FUNDING", "USDT", f"-{debit}"))
    unbalanced = postings_event(("CASH_AVAILABLE", "USDT", debit), ("FUNDING", credit_currency, credit), ts=2)
    with localcontext(prec=3):
        with open_ledger(ledger_path, "replay", ("example",)) as ledger:
            with pytest.raises(LedgerError, match="借贷不平"):
                ledger.record([balanced, unbalanced])
            assert ledger.balances("example") == {}
            assert read_journal(ledger_path).events == ()
            ledger.record([balanced])
            assert ledger.balances("example")[("USDT", "CASH_AVAILABLE")] == Decimal(debit)

    journal = read_journal(ledger_path)
    assert len(journal.events) == len(journal.entries) == 1
    assert journal.entries[0].postings == balanced.postings


def test_existing_run_ledger_cannot_be_reopened_for_writing(tmp_path):
    ledger_path = tmp_path / "ledger.sqlite"
    event = postings_event(("CASH_AVAILABLE", "USDT", "1"), ("FUNDING", "USDT", "-1"))
    with open_ledger(ledger_path, "replay", ("example",)) as ledger:
        ledger.record([event])

    with pytest.raises(LedgerError, match="已存在"):
        with open_ledger(ledger_path, "next-run", ("example",)):
            pytest.fail("应为每次回放分配新账本")
    assert read_journal(ledger_path).entries[0].postings == event.postings
