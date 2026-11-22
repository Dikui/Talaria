"""trading/02：市场包按标记价估值线性永续保证金。"""

from decimal import Decimal as D

import pytest

from talaria.foundation.margin import MarginPortfolio, MarginPosition
from talaria.packs.crypto_perp import CRYPTO_PERP_PACK

pytestmark = [pytest.mark.small, pytest.mark.spec]

BTC = "BTC-USDT-PERP"
ETH = "ETH-USDT-PERP"


@pytest.mark.parametrize(("direction", "expected"), [("LONG", "80.4"), ("SHORT", "119.4")])
def test_isolated_liquidation_solves_equity_equals_maintenance(direction, expected):
    valuation = CRYPTO_PERP_PACK.margin_valuation()
    price = valuation.isolated_liquidation_price(BTC, direction, D("2"), D("100"), D("40"), as_of=5)
    assert price.quantize(D("0.1")) == D(expected)


def test_cross_margin_revalues_all_positions_at_marks():
    valuation = CRYPTO_PERP_PACK.margin_valuation()
    portfolio = MarginPortfolio(D("100"), D("5"), (
        MarginPosition(BTC, "LONG", D("2"), D("100")),
        MarginPosition(ETH, "SHORT", D("1"), D("100")),
    ))
    marks = {BTC: D("90"), ETH: D("110")}
    estimate = valuation.cross_margin(portfolio, marks, as_of=5)
    assert (estimate.equity, estimate.initial_margin, estimate.maintenance_margin) == (
        D("70"), D("58"), D("1.45"),
    )
    with pytest.raises(ValueError, match=ETH):
        valuation.cross_margin(portfolio, {BTC: D("90")}, as_of=5)
