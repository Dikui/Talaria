"""Pine v6 指标的独立参考实现，只用于生成与复核黄金向量。

生产代码不得导入本模块；这里刻意不依赖 ``talaria.foundation.operators``，
以便向量能发现生产算子核的语义漂移。生成命令::

    .venv/bin/python -m tests.foundation.operators.pine_reference --write

语义依据见 ``docs/research/tradingview-borrowing-list.md``（仅本地）/ADR-0010：
EMA 第一根有效柱用收盘价播种；ATR 首根 TR 为 high-low，种子为前 N 根 TR 的
SMA，之后用 Wilder RMA；VWAP 按 UTC 自然日锚定，首个锚点之前为 na。
"""

from __future__ import annotations

import json
import sys
from decimal import Decimal
from pathlib import Path

VECTOR_DIR = Path(__file__).resolve().parents[3] / "contracts" / "operator_vectors"

#: 全向量共用同一组柱：3 根正常柱后一次 FEED_GAP，再跨过一个 UTC 日界。
BAR_SPECS: list[dict[str, object]] = [
    {"offset": 0, "high": "11", "low": "9", "close": "10", "volume": "1", "status": "NORMAL"},
    {"offset": 1, "status": "FEED_GAP"},
    {"offset": 2, "high": "13", "low": "11", "close": "12", "volume": "1", "status": "NORMAL"},
    {"offset": 3, "high": "15", "low": "13", "close": "14", "volume": "2", "status": "NORMAL"},
    {"offset": 4, "high": "14", "low": "14", "close": "14", "volume": "0", "status": "NO_TRADES"},
    {"offset": 5, "high": "17", "low": "15", "close": "16", "volume": "1", "status": "NORMAL"},
    {"offset": 6, "high": "16", "low": "14", "close": "15", "volume": "1", "status": "NORMAL"},
    {"offset": 7, "high": "15", "low": "13", "close": "14", "volume": "1", "status": "NORMAL"},
    {"offset": 8, "high": "18", "low": "16", "close": "17", "volume": "1", "status": "NORMAL"},
    {"offset": 9, "high": "19", "low": "17", "close": "18", "volume": "1", "status": "NORMAL"},
    {"offset": 10, "high": "17", "low": "15", "close": "16", "volume": "1", "status": "NORMAL"},
    {"offset": 11, "high": "21", "low": "19", "close": "20", "volume": "1", "status": "NORMAL"},
    {"offset": 107, "high": "25", "low": "23", "close": "24", "volume": "2", "status": "NORMAL"},
]


def _bar_payload(spec: dict[str, object]) -> dict[str, object]:
    offset = int(spec["offset"])  # type: ignore[arg-type]
    if spec.get("status") == "FEED_GAP":
        return {
            "offset": offset,
            "open": None,
            "high": None,
            "low": None,
            "close": None,
            "volume": None,
            "turnover": None,
            "status": "FEED_GAP",
        }
    close = Decimal(str(spec["close"]))
    volume = Decimal(str(spec["volume"]))
    return {
        "offset": offset,
        "open": close,
        "high": Decimal(str(spec["high"])),
        "low": Decimal(str(spec["low"])),
        "close": close,
        "volume": volume,
        "turnover": volume * close,
        "status": spec["status"],
    }


def _decimal(value: object) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def ema_series(bars: list[dict[str, object]], period: int) -> list[Decimal | None]:
    alpha = Decimal(2) / Decimal(period + 1)
    ema: Decimal | None = None
    out: list[Decimal | None] = []
    for bar in bars:
        if bar["status"] == "FEED_GAP":
            ema = None
            out.append(None)
            continue
        close = _decimal(bar["close"])
        assert close is not None
        ema = close if ema is None else alpha * close + (1 - alpha) * ema
        out.append(ema)
    return out


def atr_series(bars: list[dict[str, object]], period: int) -> list[Decimal | None]:
    alpha = Decimal(1) / Decimal(period)
    previous_close: Decimal | None = None
    seed: list[Decimal] = []
    atr: Decimal | None = None
    out: list[Decimal | None] = []
    for bar in bars:
        if bar["status"] == "FEED_GAP":
            previous_close = None
            seed.clear()
            atr = None
            out.append(None)
            continue
        close = _decimal(bar["close"])
        high = _decimal(bar["high"])
        low = _decimal(bar["low"])
        assert close is not None and high is not None and low is not None
        if previous_close is None:
            true_range = high - low
        else:
            true_range = max(high - low, abs(high - previous_close), abs(low - previous_close))
        previous_close = close
        if atr is None:
            seed.append(true_range)
            if len(seed) < period:
                out.append(None)
                continue
            atr = sum(seed) / Decimal(period)
            seed.clear()
            out.append(atr)
            continue
        atr = alpha * true_range + (1 - alpha) * atr
        out.append(atr)
    return out


def vwap_series(
    bars: list[dict[str, object]], anchor: str = "session"
) -> list[Decimal | None]:
    session_anchored = anchor == "session"
    anchored = not session_anchored
    cumulative_pv = Decimal(0)
    cumulative_volume = Decimal(0)
    day: int | None = None
    out: list[Decimal | None] = []
    for bar in bars:
        if bar["status"] == "FEED_GAP":
            cumulative_pv = Decimal(0)
            cumulative_volume = Decimal(0)
            day = None
            anchored = not session_anchored
            out.append(None)
            continue
        offset = int(bar["offset"])  # type: ignore[arg-type]
        bar_day = (1_700_000_000_000 + offset * 60_000) // 86_400_000
        if session_anchored:
            if day is None:
                day = bar_day
                out.append(None)
                continue
            if bar_day != day:
                day = bar_day
                cumulative_pv = Decimal(0)
                cumulative_volume = Decimal(0)
                anchored = True
            if not anchored:
                out.append(None)
                continue
        volume = _decimal(bar["volume"]) or Decimal(0)
        if volume != 0:
            typical = (
                _decimal(bar["high"]) + _decimal(bar["low"]) + _decimal(bar["close"])
            ) / Decimal(3)  # type: ignore[operator]
            cumulative_pv += typical * volume
            cumulative_volume += volume
        out.append(
            None if cumulative_volume == 0 else cumulative_pv / cumulative_volume
        )
    return out


def _payload(values: list[Decimal | None]) -> list[dict[str, str | None]]:
    return [
        {"value": None if value is None else format(value, "f")} for value in values
    ]


def _leading_null_count(values: list[Decimal | None]) -> int:
    for index, value in enumerate(values):
        if value is not None:
            return index
    return len(values)


def _write(name: str, operator: str, params: dict[str, object], values: list[Decimal | None], post_gap_warmup: int) -> None:
    payload = {
        "operator": operator,
        "params": params,
        "warmup_bars": _leading_null_count(values),
        "post_gap_warmup": post_gap_warmup,
        "bars": [
            {
                key: (format(value, "f") if isinstance(value, Decimal) else value)
                for key, value in _bar_payload(spec).items()
            }
            for spec in BAR_SPECS
        ],
        "expected": _payload(values),
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    (VECTOR_DIR / f"{name}.json").write_text(text, encoding="utf-8")


def write_vectors() -> None:
    bars = [_bar_payload(spec) for spec in BAR_SPECS]
    _write("ema", "EMA", {"period": 6}, ema_series(bars, 6), 0)
    _write("atr", "ATR", {"period": 3}, atr_series(bars, 3), 2)
    _write(
        "vwap",
        "VWAP",
        {"period": 1, "anchor": "session"},
        vwap_series(bars, "session"),
        10,
    )


if __name__ == "__main__":
    if "--write" not in sys.argv:
        raise SystemExit("用法：python -m tests.foundation.operators.pine_reference --write")
    write_vectors()
    print("operator vectors regenerated")
