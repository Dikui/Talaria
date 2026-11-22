"""Python 组件接口与研究模式结果。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Literal, Protocol

from talaria.foundation.collaboration.contracts import Abstain, SetupInsight
from talaria.foundation.indicators import IndicatorPoint

if TYPE_CHECKING:
    from talaria.components.runtime import Ctx

type Series = tuple[IndicatorPoint, ...]
type ComponentOutput = Literal["series", "signal_events", "strategy_decision"]


@dataclass(frozen=True, slots=True)
class ComponentMeta:
    """参数使用 JSON Schema 表达默认值、数值范围与离散选项。"""

    name: str
    version: str
    inputs: tuple[str, ...]
    parameters_schema: Mapping[str, object]
    output_type: ComponentOutput


@dataclass(frozen=True, slots=True)
class SignalEvent:
    timestamp: int
    name: str
    values: Mapping[str, Decimal | str | bool | None] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ControllerPatch:
    """在有效期内应用到目标策略的参数补丁。"""

    parameters: Mapping[str, Decimal | int | str | bool]
    valid_until_ts: int
    dsh_turn_id: str


type StrategyDecision = SetupInsight | ControllerPatch | Abstain


class Component(Protocol):
    @property
    def meta(self) -> ComponentMeta: ...


class Indicator(Component, Protocol):
    def compute(self, ctx: Ctx) -> Series: ...


class Signal(Component, Protocol):
    def evaluate(self, ctx: Ctx) -> list[SignalEvent]: ...


class Strategy(Component, Protocol):
    def on_bar(self, ctx: Ctx) -> StrategyDecision: ...
