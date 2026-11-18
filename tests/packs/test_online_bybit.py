"""真实 Bybit 连通性冒烟：默认跳过，仅在沙箱外显式 -m online 时运行。"""

from __future__ import annotations

import pytest

from talaria.packs.crypto_perp.connector import BybitLinearConnector

pytestmark = pytest.mark.online


def test_bybit_public_kline_is_reachable() -> None:
    connector = BybitLinearConnector(timeout=5.0, retries=0)
    try:
        bars = connector.fetch_bars(symbol="BTCUSDT", timeframe="1m", limit=2)
    finally:
        connector.close()
    assert bars
    assert bars[-1].ts_event > 0
