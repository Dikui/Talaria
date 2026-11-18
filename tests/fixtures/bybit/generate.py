"""生成确定性的 Bybit V5 Linear 原始 K 线响应 fixture。

用法：`uv run python tests/fixtures/bybit/generate.py`

数据完全由固定种子推导，禁止手工编辑生成的 JSON。文件布局与 `--fixture-dir`
的消费约定一致：`<fixture_dir>/<SYMBOL>/<timeframe>.json`。

刻意保留两个结构：
- BTCUSDT 1m 缺失 5 根连续 K 线（用于验证 `FEED_GAP` 插入）；
- BTCUSDT 1m 有一根零成交量平盘 K 线（用于验证 `NO_TRADES`）。
"""

from __future__ import annotations

import json
import random
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

HERE = Path(__file__).resolve().parent

SPECS = [
    # symbol, timeframe, bars, start price, price decimals, qty decimals, base UTC
    ("BTCUSDT", "1m", 905, Decimal("68000"), 1, 3, "2026-11-01T00:00:00Z"),
    ("BTCUSDT", "5m", 400, Decimal("68000"), 1, 3, "2026-11-01T00:00:00Z"),
    ("BTCUSDT", "1h", 200, Decimal("68000"), 1, 3, "2026-11-01T00:00:00Z"),
    ("ETHUSDT", "1m", 400, Decimal("3500"), 2, 3, "2026-11-01T00:00:00Z"),
    ("ETHUSDT", "5m", 240, Decimal("3500"), 2, 3, "2026-11-01T00:00:00Z"),
    ("1000BONKUSDT", "1m", 300, Decimal("0.0235"), 6, 1, "2026-11-01T00:00:00Z"),
]

INTERVAL_MS = {
    "1m": 60_000,
    "5m": 300_000,
    "1h": 3_600_000,
}


def _iso_to_ms(text: str) -> int:
    return int(datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC).timestamp() * 1000)


def build_bars(
    *,
    symbol: str,
    timeframe: str,
    count: int,
    start_price: Decimal,
    price_decimals: int,
    qty_decimals: int,
    base_ts: int,
    rng: random.Random,
) -> list[list[str]]:
    interval_ms = INTERVAL_MS[timeframe]
    price_quant = Decimal(1).scaleb(-price_decimals)
    qty_quant = Decimal(1).scaleb(-qty_decimals)
    price = start_price
    rows: list[list[str]] = []
    for index in range(count):
        open_price = price
        drift = Decimal(str(rng.gauss(0, 0.0007))) * open_price
        close_price = (open_price + drift).quantize(price_quant)
        if close_price <= 0:
            close_price = price_quant
        high = (max(open_price, close_price) + abs(Decimal(str(rng.gauss(0, 0.0004)))) * open_price).quantize(price_quant)
        low = (min(open_price, close_price) - abs(Decimal(str(rng.gauss(0, 0.0004)))) * open_price).quantize(price_quant)
        if low <= 0:
            low = price_quant
        volume = Decimal(str(round(rng.uniform(0.5, 20.0), qty_decimals))).quantize(qty_quant)
        turnover = (volume * (open_price + close_price) / 2).quantize(Decimal("0.01"))
        rows.append(
            [
                str(base_ts + index * interval_ms),
                format(open_price, "f"),
                format(high, "f"),
                format(low, "f"),
                format(close_price, "f"),
                format(volume, "f"),
                format(turnover, "f"),
            ]
        )
        price = close_price
    return rows


def _apply_structures(rows: list[list[str]]) -> list[list[str]]:
    """在 BTCUSDT 1m 上制造一个断流空槽和一根零量平盘柱。"""
    # 零量平盘：开高低收等于前一根收盘，成交量为 0。
    flat_index = 820
    previous_close = rows[flat_index - 1][4]
    rows[flat_index] = [
        rows[flat_index][0],
        previous_close,
        previous_close,
        previous_close,
        previous_close,
        "0",
        "0",
    ]
    # 断流：删除 780..784 共 5 根，前后间隔 6 个周期。
    for index in range(780, 785):
        rows[index] = ["__DROP__"]
    return [row for row in rows if row != ["__DROP__"]]


def _write_fixture(path: Path, symbol: str, rows_descending: list[list[str]]) -> None:
    payload = {
        "retCode": 0,
        "retMsg": "OK",
        "result": {
            "category": "linear",
            "symbol": symbol,
            "list": rows_descending,
        },
        "time": 1_793_491_200_000,
    }
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def main() -> None:
    for symbol, timeframe, count, start_price, price_decimals, qty_decimals, base_iso in SPECS:
        rng = random.Random(f"talaria-02-{symbol}-{timeframe}")
        base_ts = _iso_to_ms(base_iso)
        rows = build_bars(
            symbol=symbol,
            timeframe=timeframe,
            count=count,
            start_price=start_price,
            price_decimals=price_decimals,
            qty_decimals=qty_decimals,
            base_ts=base_ts,
            rng=rng,
        )
        if (symbol, timeframe) == ("BTCUSDT", "1m"):
            rows = _apply_structures(rows)
        # Bybit 原始响应按开盘时间降序返回。
        rows_descending = list(reversed(rows))
        _write_fixture(HERE / symbol / f"{timeframe}.json", symbol, rows_descending)
        print(f"{symbol:<14} {timeframe:<4} {len(rows_descending):>4} bars")


if __name__ == "__main__":
    main()
