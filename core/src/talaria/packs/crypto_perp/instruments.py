"""加密永续市场包的标的、能力组合与数据源。"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Callable, Sequence

from talaria.foundation.derivatives import DerivativeKind
from talaria.foundation.market import (
    CarryTrait,
    DerivativeCapabilityTrait,
    FeedCapabilityTrait,
    InstrumentSpec,
    MarginTrait,
    OrderConstraintTrait,
    SessionTrait,
    TraitBundle,
    TradingSession,
)
from talaria.packs.crypto_perp.connector import DEFAULT_BASE_URL, BybitLinearConnector
from talaria.packs.crypto_perp.margin import CryptoPerpMarginValuation
from talaria.packs.crypto_perp.prompt import MARKET_PACK_SKILL
from talaria.packs.crypto_perp.stream import BybitPublicStream
from talaria.packs.crypto_perp.trade_archive import open_trade_archive

_INSTRUMENTS: tuple[InstrumentSpec, ...] = (
    InstrumentSpec(
        instrument_id="BTC-USDT-PERP",
        venue="bybit",
        symbol="BTCUSDT",
        asset_class="crypto_perp",
        tick_size=Decimal("0.1"),
        qty_step=Decimal("0.001"),
        min_qty=Decimal("0.001"),
        price_precision=1,
        qty_precision=3,
    ),
    InstrumentSpec(
        instrument_id="ETH-USDT-PERP",
        venue="bybit",
        symbol="ETHUSDT",
        asset_class="crypto_perp",
        tick_size=Decimal("0.01"),
        qty_step=Decimal("0.01"),
        min_qty=Decimal("0.01"),
        price_precision=2,
        qty_precision=2,
    ),
    InstrumentSpec(
        instrument_id="SOL-USDT-PERP",
        venue="bybit",
        symbol="SOLUSDT",
        asset_class="crypto_perp",
        tick_size=Decimal("0.01"),
        qty_step=Decimal("0.1"),
        min_qty=Decimal("0.1"),
        price_precision=2,
        qty_precision=1,
    ),
    InstrumentSpec(
        instrument_id="1000BONK-USDT-PERP",
        venue="bybit",
        symbol="1000BONKUSDT",
        asset_class="crypto_perp",
        tick_size=Decimal("0.000001"),
        qty_step=Decimal("1"),
        min_qty=Decimal("1"),
        price_precision=6,
        qty_precision=0,
    ),
)

_TRAITS = TraitBundle(
    feed=FeedCapabilityTrait(realtime=True, polling=True, history=True),
    margin=MarginTrait(margin_asset="USDT", max_leverage=Decimal("100")),
    carry=CarryTrait(funding_interval_hours=8),
    session=SessionTrait(
        session_kind="24x7",
        timezone="UTC",
        day_start="00:00",
        sessions=(
            TradingSession(name="asia", start="00:00", end="08:00"),
            TradingSession(name="london", start="07:00", end="16:00"),
            TradingSession(name="new_york", start="12:00", end="21:00"),
        ),
    ),
    order_constraint=OrderConstraintTrait(post_only=True),
    derivatives=DerivativeCapabilityTrait(
        kinds=(
            DerivativeKind.MARK_PRICE,
            DerivativeKind.FUNDING_RATE,
            DerivativeKind.OPEN_INTEREST,
        ),
        history=True,
        realtime=True,
    ),
)


class CryptoPerpPack:
    """24x7 USDT 永续市场包。"""

    name = "crypto_perp"
    raw_archive_id = "bybit_linear"
    market_skill = MARKET_PACK_SKILL
    default_fixture_dir = Path("tests/fixtures/bybit")

    def __init__(self, instruments: Sequence[InstrumentSpec] = _INSTRUMENTS) -> None:
        self._instruments = tuple(instruments)
        self._by_id = {spec.instrument_id: spec for spec in self._instruments}

    def instruments(self) -> Sequence[InstrumentSpec]:
        return self._instruments

    def instrument(self, instrument_id: str) -> InstrumentSpec | None:
        return self._by_id.get(instrument_id)

    def traits(self, instrument_id: str) -> TraitBundle:
        if instrument_id not in self._by_id:
            raise KeyError(instrument_id)
        return _TRAITS

    def margin_valuation(self) -> CryptoPerpMarginValuation:
        return CryptoPerpMarginValuation(self._margin_trait)

    def _margin_trait(self, instrument_id: str) -> MarginTrait:
        return self.traits(instrument_id).margin

    def create_history_connector(
        self, *, base_url: str | None = None
    ) -> BybitLinearConnector:
        return BybitLinearConnector(DEFAULT_BASE_URL if base_url is None else base_url)

    def create_realtime_stream(self, *, now_ms: Callable[[], int]) -> BybitPublicStream:
        return BybitPublicStream(instruments=self.instruments(), now_ms=now_ms)

    def open_trade_archive(self, instrument: InstrumentSpec, day: date, now_ms: Callable[[], int]):
        return open_trade_archive(instrument, day, now_ms)


CRYPTO_PERP_PACK = CryptoPerpPack()
