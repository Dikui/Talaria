"""L0 契约：十进制字符串、时点校验、Trait 组合与核心层隔离。"""

from __future__ import annotations

import ast
import json
from dataclasses import FrozenInstanceError
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from talaria.foundation.bars import BarStatus, CompactBar
from talaria.foundation.market import (
    CarryTrait,
    FeedCapabilityTrait,
    InstrumentSpec,
    MarginTrait,
    OrderConstraintTrait,
    SessionTrait,
    TraitBundle,
)
from talaria.foundation.types import DECIMAL_PATTERN, PrecisionTier, TemporalStamp


def _contract_bar(**overrides: object) -> CompactBar:
    payload = {
        "timestamp": 1_700_000_000_000,
        "open": "101.10",
        "high": "102.20",
        "low": "100.00",
        "close": "101.50",
        "volume": "12.345",
        "turnover": "1250.75",
        "status": "NORMAL",
        "ts_available": 1_700_000_060_000,
        "precision": "SECOND_VERIFIED",
    }
    payload.update(overrides)
    return CompactBar(**payload)


def _collect_decimal_schemas(node: object) -> list[dict]:
    found: list[dict] = []
    if isinstance(node, dict):
        if node.get("type") == "string" and node.get("pattern") == DECIMAL_PATTERN:
            found.append(node)
        for value in node.values():
            found.extend(_collect_decimal_schemas(value))
    elif isinstance(node, list):
        for value in node:
            found.extend(_collect_decimal_schemas(value))
    return found


def test_decimal_fields_serialize_to_decimal_strings() -> None:
    bar = _contract_bar()
    serialized = json.loads(bar.model_dump_json())
    assert serialized["open"] == "101.10"
    assert serialized["volume"] == "12.345"
    assert isinstance(serialized["turnover"], str)
    assert bar.open == Decimal("101.10")

    instrument = InstrumentSpec(
        instrument_id="BTC-USDT-PERP",
        venue="bybit",
        symbol="BTCUSDT",
        asset_class="crypto_perp",
        tick_size="0.1",
        qty_step="0.001",
        min_qty="0.001",
        price_precision=1,
        qty_precision=3,
    )
    payload = json.loads(instrument.model_dump_json())
    assert payload["tick_size"] == "0.1"
    assert payload["qty_step"] == "0.001"
    assert payload["min_qty"] == "0.001"


def test_decimal_schema_is_string_pattern_not_number() -> None:
    bar_schema = CompactBar.model_json_schema()
    open_schema = bar_schema["properties"]["open"]
    decimal_schemas = _collect_decimal_schemas(open_schema)
    assert decimal_schemas, open_schema
    assert all(schema["type"] == "string" for schema in decimal_schemas)

    instrument_schema = InstrumentSpec.model_json_schema()
    tick_schema = instrument_schema["properties"]["tick_size"]
    assert tick_schema["type"] == "string"
    assert tick_schema["pattern"] == DECIMAL_PATTERN
    assert "number" not in json.dumps(instrument_schema)
    assert '"integer"' not in json.dumps(
        {key: value for key, value in instrument_schema["properties"].items() if key in {"tick_size", "qty_step", "min_qty"}}
    )


def test_temporal_stamp_requires_available_after_event() -> None:
    with pytest.raises(ValidationError, match="ts_available"):
        TemporalStamp(
            ts_event=100,
            ts_available=99,
            precision=PrecisionTier.SECOND_VERIFIED,
            ts_ingested=100,
            ts_committed=100,
        )


def test_temporal_stamp_requires_commit_after_ingest() -> None:
    with pytest.raises(ValidationError, match="ts_committed"):
        TemporalStamp(
            ts_event=100,
            ts_available=100,
            precision=PrecisionTier.SECOND_VERIFIED,
            ts_ingested=101,
            ts_committed=100,
        )


def test_temporal_stamp_allows_missing_published() -> None:
    stamp = TemporalStamp(
        ts_event=100,
        ts_available=100,
        precision=PrecisionTier.SESSION_BOUND,
        ts_ingested=101,
        ts_committed=102,
    )
    assert stamp.ts_published is None
    assert stamp.precision is PrecisionTier.SESSION_BOUND


def test_trait_bundle_combines_five_capabilities() -> None:
    bundle = TraitBundle(
        feed=FeedCapabilityTrait(realtime=True, polling=True, history=True),
        margin=MarginTrait(margin_asset="USDT", max_leverage=Decimal("100")),
        carry=CarryTrait(funding_interval_hours=8),
        session=SessionTrait(session_kind="24x7", timezone="UTC"),
        order_constraint=OrderConstraintTrait(post_only=True),
    )
    assert bundle.feed.history is True
    assert bundle.margin.max_leverage == Decimal("100")
    assert bundle.carry.funding_interval_hours == 8
    assert bundle.session.session_kind == "24x7"
    assert bundle.order_constraint.post_only is True
    with pytest.raises(FrozenInstanceError):
        bundle.feed.realtime = False  # type: ignore[misc]


def test_instrument_spec_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        InstrumentSpec(
            instrument_id="BTC-USDT-PERP",
            venue="bybit",
            symbol="BTCUSDT",
            asset_class="crypto_perp",
            tick_size="0.1",
            qty_step="0.001",
            min_qty="0.001",
            price_precision=1,
            qty_precision=3,
            extra_field=True,
        )


def test_compact_bar_gap_fields_must_be_null() -> None:
    gap = _contract_bar(
        status=BarStatus.FEED_GAP,
        open=None,
        high=None,
        low=None,
        close=None,
        volume=None,
        turnover=None,
    )
    assert gap.status is BarStatus.FEED_GAP
    with pytest.raises(ValidationError, match="FEED_GAP"):
        _contract_bar(status=BarStatus.FEED_GAP, open="101.10")


def test_core_packages_do_not_import_market_packs(repo_root: Path) -> None:
    offenders: list[str] = []
    for package in ("foundation", "perception"):
        for path in (repo_root / "core/src/talaria" / package).rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name == "talaria.packs" or alias.name.startswith("talaria.packs."):
                            offenders.append(f"{path}:{node.lineno} import {alias.name}")
                elif isinstance(node, ast.ImportFrom):
                    module = node.module or ""
                    if module == "talaria.packs" or module.startswith("talaria.packs."):
                        offenders.append(f"{path}:{node.lineno} from {module}")
    assert offenders == []
