"""指标标识、参数与固定语义（唯一一份实现）。"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any, Mapping


class IndicatorName(StrEnum):
    """技术指标与衍生品指标；全部共用同一算子核。"""

    EMA = "EMA"
    SMA = "SMA"
    RSI = "RSI"
    ATR = "ATR"
    BOLL = "BOLL"
    VWAP = "VWAP"
    FUNDING_QUANTILE = "FUNDING_QUANTILE"
    OI_DIVERGENCE = "OI_DIVERGENCE"
    NET_ODDS = "NET_ODDS"


#: 各指标的默认周期；BOLL 同时带默认标准差倍数。
DEFAULT_PERIODS: dict[IndicatorName, int] = {
    IndicatorName.EMA: 20,
    IndicatorName.SMA: 20,
    IndicatorName.RSI: 14,
    IndicatorName.ATR: 14,
    IndicatorName.BOLL: 20,
    IndicatorName.VWAP: 1,
    IndicatorName.FUNDING_QUANTILE: 30,
    IndicatorName.OI_DIVERGENCE: 30,
    IndicatorName.NET_ODDS: 20,
}

#: 单值指标输出 ``value``；BOLL 输出三轨。
CHANNELS: dict[IndicatorName, tuple[str, ...]] = {
    IndicatorName.EMA: ("value",),
    IndicatorName.SMA: ("value",),
    IndicatorName.RSI: ("value",),
    IndicatorName.ATR: ("value",),
    IndicatorName.BOLL: ("mid", "upper", "lower"),
    IndicatorName.VWAP: ("value",),
    IndicatorName.FUNDING_QUANTILE: ("value",),
    IndicatorName.OI_DIVERGENCE: ("value",),
    IndicatorName.NET_ODDS: ("value",),
}

DERIVATIVE_INDICATORS = frozenset(
    {
        IndicatorName.FUNDING_QUANTILE,
        IndicatorName.OI_DIVERGENCE,
        IndicatorName.NET_ODDS,
    }
)

SUB_PANE_NAMES = frozenset(
    {
        IndicatorName.RSI,
        IndicatorName.ATR,
        IndicatorName.FUNDING_QUANTILE,
        IndicatorName.OI_DIVERGENCE,
        IndicatorName.NET_ODDS,
    }
)
VWAP_ANCHORS = ("snapshot", "session")
MAX_PERIOD = 5000


def pane_for(name: IndicatorName) -> str:
    """图表窗格：主图叠加均线类指标，震荡与衍生品指标单独副图。"""
    return "sub" if name in SUB_PANE_NAMES else "main"


@dataclass(frozen=True, slots=True)
class IndicatorSpec:
    """一个指标实例的不可变参数；默认值由指标名决定。"""

    name: IndicatorName
    period: int | None = None
    std_dev: Decimal | None = None
    anchor: str | None = None

    def __post_init__(self) -> None:
        try:
            name = IndicatorName(self.name)
        except ValueError as exc:
            raise ValueError(f"未知指标：{self.name}") from exc
        period = DEFAULT_PERIODS[name] if self.period is None else int(self.period)
        if not 1 <= period <= MAX_PERIOD:
            raise ValueError(f"{name} 的 period 必须在 1..{MAX_PERIOD} 之间")
        if name is IndicatorName.BOLL:
            if period < 2:
                raise ValueError("BOLL 的 period 至少为 2")
            std_dev = Decimal("2") if self.std_dev is None else Decimal(self.std_dev)
            if not std_dev.is_finite() or std_dev <= 0:
                raise ValueError("BOLL 的 std_dev 必须为正数")
        else:
            std_dev = None
        if name is IndicatorName.VWAP:
            anchor = "snapshot" if self.anchor is None else str(self.anchor)
            if anchor not in VWAP_ANCHORS:
                raise ValueError(f"VWAP 的 anchor 只能是 {'/'.join(VWAP_ANCHORS)}")
        else:
            anchor = None
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "period", period)
        object.__setattr__(self, "std_dev", std_dev)
        object.__setattr__(self, "anchor", anchor)

    @property
    def pane(self) -> str:
        return pane_for(self.name)

    @property
    def channels(self) -> tuple[str, ...]:
        return CHANNELS[self.name]


def spec_from_params(
    name: IndicatorName | str,
    params: Mapping[str, Any] | None = None,
) -> IndicatorSpec:
    """把 API/前端传入的参数规范化为 ``IndicatorSpec``，非法参数抛 ``ValueError``。"""
    try:
        indicator = IndicatorName(name)
    except ValueError as exc:
        raise ValueError(f"未知指标：{name}") from exc
    raw = dict(params or {})
    unknown = set(raw) - {"period", "std_dev", "anchor"}
    if unknown:
        raise ValueError(f"{indicator} 不接受参数：{', '.join(sorted(unknown))}")
    period = raw.get("period")
    if period is not None:
        if isinstance(period, bool) or not isinstance(period, (int, Decimal)):
            raise ValueError(f"{indicator} 的 period 必须是整数")
        if isinstance(period, Decimal) and period != period.to_integral_value():
            raise ValueError(f"{indicator} 的 period 必须是整数")
        period = int(period)
    std_dev = raw.get("std_dev")
    if std_dev is not None:
        try:
            std_dev = Decimal(str(std_dev))
        except InvalidOperation as exc:
            raise ValueError("BOLL 的 std_dev 必须是十进制数") from exc
    anchor = raw.get("anchor")
    if anchor is not None and not isinstance(anchor, str):
        raise ValueError(f"{indicator} 的 anchor 必须是字符串")
    if anchor is not None and indicator is not IndicatorName.VWAP:
        raise ValueError(f"{indicator} 不接受 anchor 参数")
    return IndicatorSpec(
        name=indicator, period=period, std_dev=std_dev, anchor=anchor
    )
