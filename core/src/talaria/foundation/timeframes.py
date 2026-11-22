"""通用时间周期与毫秒换算。"""

from __future__ import annotations

TIMEFRAME_MS: dict[str, int] = {
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
    "1d": 86_400_000,
}

SUPPORTED_TIMEFRAMES: tuple[str, ...] = tuple(TIMEFRAME_MS)


def timeframe_ms(timeframe: str) -> int:
    """返回周期毫秒数；未知周期抛 ``KeyError``。"""
    return TIMEFRAME_MS[timeframe]


def last_closed_bar_start(now_ms: int, interval_ms: int) -> int:
    """返回当前时刻最后一根已结束柱的事件时间（开盘槽）。"""
    return (now_ms // interval_ms - 1) * interval_ms
