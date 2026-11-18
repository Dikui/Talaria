"""加密永续市场包：四个标的、五项 Trait、核心层隔离。"""

from __future__ import annotations

from decimal import Decimal

import pytest

from talaria.packs.crypto_perp import CRYPTO_PERP_PACK

pytestmark = pytest.mark.small

EXPECTED_IDS = {
    "BTC-USDT-PERP",
    "ETH-USDT-PERP",
    "SOL-USDT-PERP",
    "1000BONK-USDT-PERP",
}


def test_pack_exposes_four_instruments_including_long_identifier() -> None:
    instruments = CRYPTO_PERP_PACK.instruments()
    assert {spec.instrument_id for spec in instruments} == EXPECTED_IDS
    assert CRYPTO_PERP_PACK.instrument("1000BONK-USDT-PERP") is not None
    assert CRYPTO_PERP_PACK.instrument("DOGE-USDT-PERP") is None


def test_every_instrument_carries_all_five_traits() -> None:
    for instrument in CRYPTO_PERP_PACK.instruments():
        traits = CRYPTO_PERP_PACK.traits(instrument.instrument_id)
        assert traits.feed.history is True
        assert traits.feed.realtime is True
        assert traits.margin.margin_asset == "USDT"
        assert traits.margin.max_leverage == Decimal("100")
        assert traits.carry.funding_interval_hours == 8
        assert traits.session.session_kind == "24x7"
        assert traits.order_constraint.post_only is True


def test_traits_for_unknown_instrument_raise_key_error() -> None:
    with pytest.raises(KeyError):
        CRYPTO_PERP_PACK.traits("UNKNOWN")


def test_instrument_precision_is_explicit_not_float() -> None:
    btc = CRYPTO_PERP_PACK.instrument("BTC-USDT-PERP")
    assert btc is not None
    assert btc.tick_size == Decimal("0.1")
    assert btc.qty_step == Decimal("0.001")
    assert btc.price_precision == 1
    bonk = CRYPTO_PERP_PACK.instrument("1000BONK-USDT-PERP")
    assert bonk is not None
    assert bonk.tick_size == Decimal("0.000001")
    assert bonk.price_precision == 6
