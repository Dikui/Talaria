"""链路 3 模拟盘契约测试：草案终态、账本投影兼容、干预命令与定仓预演。"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest
from pydantic import ValidationError

from talaria.foundation.paper import (
    DraftDiscardRequest,
    DraftReceipt,
    DraftRecord,
    DraftStatus,
    ExitBarrierSpec,
    MarginMode,
    PaperAccountState,
    PaperAccountView,
    PaperCommand,
    PaperCommandKind,
    PaperCommandPayload,
    PaperCommandReceipt,
    PaperCommandRecord,
    PaperFeedStatus,
    PaperFill,
    PaperFillKind,
    PaperProcessInfo,
    PaperProcessState,
    PaperRound,
    SizingPreviewRequest,
    SizingPreviewResponse,
    TradingState,
    TrailingStopSpec,
)


def _sample_draft_content() -> dict[str, object]:
    return {
        "sub_account_id": "sa-breakout",
        "instrument_id": "BTC-USDT-PERP",
        "snapshot_id": "snap_1",
        "direction": "LONG",
        "entry_low": "64500.0",
        "entry_high": "64600.0",
        "invalidation_stop": "64000.0",
        "take_profit": ["66000.0"],
    }


def test_draft_status_includes_discarded_and_expired_and_discard_request() -> None:
    assert [status.value for status in DraftStatus] == [
        "STAGED",
        "MODIFIED",
        "CONFIRMED",
        "DISCARDED",
        "EXPIRED",
    ]
    for status in (DraftStatus.DISCARDED, DraftStatus.EXPIRED):
        record = DraftRecord(
            draft_id="drf_1",
            version=1,
            content_hash="a" * 64,
            content=_sample_draft_content(),
            status=status,
            claimed=False,
        )
        assert record.status is status
        assert json.loads(record.model_dump_json())["status"] == status.value

    discard = DraftDiscardRequest(version=2, discarded_by="trader", reason="price moved away")
    assert discard.version == 2
    assert discard.discarded_by == "trader"
    minimal_discard = DraftDiscardRequest(version=1)
    assert minimal_discard.discarded_by is None
    assert minimal_discard.reason is None


def test_draft_receipt_omits_quantity_when_none_and_serializes_decimal_str() -> None:
    legacy = DraftReceipt(draft_id="drf_1", version=1, accepted=False, reason="DRAFT_STALE")
    dumped = json.loads(legacy.model_dump_json())
    assert "quantity" not in dumped
    assert dumped == {
        "draft_id": "drf_1",
        "version": 1,
        "accepted": False,
        "plan_id": None,
        "reason": "DRAFT_STALE",
        "snapshot_id": None,
    }

    accepted = DraftReceipt(
        draft_id="drf_1",
        version=1,
        accepted=True,
        plan_id="pln_1",
        snapshot_id="snap_1",
        quantity="0.125",
    )
    accepted_json = json.loads(accepted.model_dump_json())
    assert accepted_json["quantity"] == "0.125"
    assert isinstance(accepted_json["quantity"], str)
    assert accepted.quantity == Decimal("0.125")


def test_legacy_paper_account_view_and_round_json_parse_cleanly() -> None:
    legacy_json = json.dumps(
        {
            "sub_account_id": "sa-one",
            "positions": [
                {
                    "round_id": "rnd_1",
                    "sub_account_id": "sa-one",
                    "instrument_id": "BTC-USDT-PERP",
                    "direction": "LONG",
                    "quantity": "0.1",
                    "stop": "64000",
                    "take_profit": ["66000"],
                    "phase": "ACTIVE_PROTECTED",
                }
            ],
            "rounds": [
                {
                    "round_id": "rnd_1",
                    "sub_account_id": "sa-one",
                    "instrument_id": "BTC-USDT-PERP",
                    "direction": "LONG",
                    "quantity": "0.1",
                    "stop": "64000",
                }
            ],
            "fills": [
                {
                    "event_id": "evt_1",
                    "round_id": "rnd_1",
                    "instrument_id": "BTC-USDT-PERP",
                    "kind": "ENTRY_FILLED",
                    "ts_event": 1_700_000_060_000,
                    "quantity": "0.1",
                    "price": "64550",
                }
            ],
        }
    )

    view = PaperAccountView.model_validate_json(legacy_json)
    assert view.sub_account_id == "sa-one"
    assert view.ledger_seq == 0
    assert view.account_state is None
    assert view.feed_status is None
    assert view.process is None
    assert len(view.positions) == 1
    assert view.positions[0].entry_low is None
    assert view.positions[0].entry_high is None
    assert view.positions[0].mark_price is None
    assert view.positions[0].unrealized_pnl is None
    assert view.fills[0].kind is PaperFillKind.ENTRY_FILLED


def test_extended_paper_account_view_serializes_all_decimals_as_strings() -> None:
    barriers = ExitBarrierSpec(
        stop="64000",
        take_profit=("66000", "67000"),
        max_holding_ms=3_600_000,
        trailing_stop=TrailingStopSpec(activation_pct="0.01", distance_pct="0.005"),
    )
    active_round = PaperRound(
        round_id="rnd_1",
        sub_account_id="sa-breakout",
        instrument_id="BTC-USDT-PERP",
        direction="LONG",
        quantity="0.25",
        stop="64000",
        take_profit=("66000", "67000"),
        phase="ACTIVE_PROTECTED",
        barriers=barriers,
        entry_low="64500",
        entry_high="64600",
        entry_price="64550",
        mark_price="65100",
        unrealized_pnl="137.50",
        opened_ts=1_700_000_060_000,
    )
    view = PaperAccountView(
        sub_account_id="sa-breakout",
        positions=[active_round],
        rounds=[active_round],
        fills=[
            PaperFill(
                event_id="evt_1",
                round_id="rnd_1",
                instrument_id="BTC-USDT-PERP",
                kind=PaperFillKind.ENTRY_FILLED,
                ts_event=1_700_000_060_000,
                quantity="0.25",
                price="64550",
                fee="9.6825",
                realized_pnl="0",
                reason="LIMIT_FILL",
            )
        ],
        ledger_seq=42,
        account_state=PaperAccountState(
            currency="USDT",
            equity="10127.8175",
            cash_available="6900.3175",
            margin_used="3227.50",
            trading_state=TradingState.ACTIVE,
            trading_state_reason=None,
            intraday_drawdown_pct="0.005",
            peak_drawdown_pct="0.012",
            open_r="0.5",
            risk_reserved="50.00",
            margin_mode=MarginMode.ISOLATED,
            leverage="5",
            r_unit_pct="0.005",
            max_single_r_pct="0.01",
            friction_buffer_pct="0.002",
            liquidation_buffer_pct="0",
            liquidation_buffer_ticks=1,
        ),
        feed_status=PaperFeedStatus(
            healthy=True,
            reduce_reason=None,
            service_warning=None,
            updated_ts=1_700_000_061_000,
        ),
        process=PaperProcessInfo(
            pid=12345,
            scheme="btc-breakout-paper",
            started_ts=1_700_000_000_000,
            heartbeat_ts=1_700_000_061_000,
            status=PaperProcessState.RUNNING,
        ),
    )

    payload = json.loads(view.model_dump_json())
    assert payload["ledger_seq"] == 42
    assert payload["account_state"]["equity"] == "10127.8175"
    assert payload["account_state"]["cash_available"] == "6900.3175"
    assert payload["account_state"]["margin_used"] == "3227.50"
    assert payload["account_state"]["intraday_drawdown_pct"] == "0.005"
    assert payload["account_state"]["peak_drawdown_pct"] == "0.012"
    assert payload["account_state"]["open_r"] == "0.5"
    assert payload["account_state"]["risk_reserved"] == "50.00"
    assert payload["positions"][0]["entry_low"] == "64500"
    assert payload["positions"][0]["mark_price"] == "65100"
    assert payload["positions"][0]["unrealized_pnl"] == "137.50"
    assert payload["process"]["status"] == "RUNNING"
    assert PaperAccountView.model_validate(payload) == view


@pytest.mark.parametrize(
    ("kind", "round_id", "payload"),
    [
        (PaperCommandKind.TIGHTEN_STOP, "rnd_1", {"stop": "64200"}),
        (PaperCommandKind.MOVE_TO_BREAKEVEN, "rnd_1", {}),
        (PaperCommandKind.MOVE_TO_BREAKEVEN, "rnd_1", {"stop": "64550"}),
        (PaperCommandKind.WIDEN_STOP, "rnd_1", {"stop": "63500", "reason": "volatility"}),
        (PaperCommandKind.MODIFY_TAKE_PROFIT, "rnd_1", {"take_profit": ["66500", "67500"]}),
        (PaperCommandKind.CANCEL_ENTRY, "rnd_1", {"reason": "manual cancel"}),
        (PaperCommandKind.CLOSE_ALL, "rnd_1", {"reason": "take profit manually"}),
        (PaperCommandKind.REDUCE_RATIO, "rnd_1", {"ratio": "0.5"}),
        (PaperCommandKind.SET_TRADING_STATE, None, {"trading_state": "REDUCING", "reason": "risk off"}),
    ],
)
def test_paper_command_accepts_all_valid_kinds(
    kind: PaperCommandKind, round_id: str | None, payload: dict[str, object]
) -> None:
    cmd = PaperCommand(
        command_id="cmd_1",
        sub_account_id="sa-breakout",
        round_id=round_id,
        kind=kind,
        payload=PaperCommandPayload(**payload),
        based_on_ledger_seq=10,
        issued_by="trader",
        issued_ts=1_700_000_062_000,
    )
    receipt = PaperCommandReceipt(
        command_id=cmd.command_id,
        sub_account_id=cmd.sub_account_id,
        accepted=True,
        reason=None,
        ledger_seq=11,
        processed_ts=1_700_000_063_000,
    )
    record = PaperCommandRecord(command=cmd, claimed=True, receipt=receipt)
    assert PaperCommandRecord.model_validate_json(record.model_dump_json()) == record


@pytest.mark.parametrize(
    ("kind", "round_id", "payload"),
    [
        (PaperCommandKind.TIGHTEN_STOP, None, {"stop": "64200"}),
        (PaperCommandKind.TIGHTEN_STOP, "rnd_1", {}),
        (PaperCommandKind.WIDEN_STOP, "rnd_1", {}),
        (PaperCommandKind.MODIFY_TAKE_PROFIT, "rnd_1", {}),
        (PaperCommandKind.REDUCE_RATIO, "rnd_1", {}),
        (PaperCommandKind.REDUCE_RATIO, "rnd_1", {"ratio": "1.5"}),
        (PaperCommandKind.CLOSE_ALL, None, {}),
        (PaperCommandKind.SET_TRADING_STATE, None, {}),
        (PaperCommandKind.SET_TRADING_STATE, "rnd_1", {"trading_state": "HALTED"}),
    ],
)
def test_paper_command_rejects_invalid_kind_payload_combinations(
    kind: PaperCommandKind, round_id: str | None, payload: dict[str, object]
) -> None:
    with pytest.raises(ValidationError):
        PaperCommand(
            command_id="cmd_bad",
            sub_account_id="sa-breakout",
            round_id=round_id,
            kind=kind,
            payload=PaperCommandPayload(**payload),
            based_on_ledger_seq=10,
            issued_by="trader",
            issued_ts=1_700_000_062_000,
        )


def test_sizing_preview_request_and_response_decimal_serialization() -> None:
    # Request intentionally allows invalid stop geometry so preview endpoint can return rejected_reason
    req = SizingPreviewRequest(
        sub_account_id="sa-breakout",
        instrument_id="BTC-USDT-PERP",
        direction="LONG",
        entry_low="64500",
        entry_high="64600",
        invalidation_stop="65000",
        take_profit=("66000",),
        override_quantity="0.1",
    )
    assert json.loads(req.model_dump_json())["override_quantity"] == "0.1"

    ok = SizingPreviewResponse(
        quantity="0.068",
        basis="risk_budget=50, worst_entry=64600, stop=64000, risk_per_unit=729.2",
        risk_amount_r="49.5856",
        risk_pct_of_equity="0.00495856",
        est_liq_price="51900",
        est_liq_distance="12100",
        est_liq_distance_pct="0.1873",
        rejected_reason=None,
        from_offline_snapshot=False,
        based_on_ledger_seq=42,
    )
    ok_json = json.loads(ok.model_dump_json())
    for field in (
        "quantity",
        "risk_amount_r",
        "risk_pct_of_equity",
        "est_liq_price",
        "est_liq_distance",
        "est_liq_distance_pct",
    ):
        assert isinstance(ok_json[field], str), field
    assert SizingPreviewResponse.model_validate(ok_json) == ok

    rejected = SizingPreviewResponse(
        rejected_reason="INVALID_STOP_GEOMETRY",
        from_offline_snapshot=True,
        based_on_ledger_seq=41,
    )
    assert rejected.quantity is None
    assert rejected.rejected_reason == "INVALID_STOP_GEOMETRY"
    assert rejected.from_offline_snapshot is True
