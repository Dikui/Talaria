"""版本化 JSON 规则契约。表达式只包含已知的运算与输入。"""

from __future__ import annotations

from collections.abc import Iterator
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field, model_validator

from talaria.foundation.derivatives import DerivativeKind
from talaria.foundation.key_levels.contracts import KeyLevelKind
from talaria.foundation.operators import CHANNELS, MAX_PERIOD, IndicatorName, spec_from_params
from talaria.foundation.types import ContractModel, DecimalStr

MAX_RULE_DEPTH = 5
MAX_RULE_NODES = 64


class AlertMetric(StrEnum):
    LAST_PRICE = "last_price"
    BID_PRICE = "bid_price"
    ASK_PRICE = "ask_price"
    MARK_PRICE = "mark_price"
    FUNDING_RATE = "funding_rate"
    OPEN_INTEREST = "open_interest"


class AlertDirection(StrEnum):
    ABOVE = "ABOVE"
    BELOW = "BELOW"


class AlertOverlayMode(StrEnum):
    TOUCH = "TOUCH"
    CLOSE_BREAK = "CLOSE_BREAK"


class AlertLevelEvent(StrEnum):
    TOUCH = "TOUCH"
    BREAK = "BREAK"
    RETEST = "RETEST"


class RuleConstant(ContractModel):
    source: Literal["constant"]
    value: DecimalStr


class RuleBar(ContractModel):
    source: Literal["bar"]
    field: Literal["open", "high", "low", "close", "volume", "turnover"]


class RuleEvent(ContractModel):
    source: Literal["event"]
    metric: AlertMetric


class RuleDerivative(ContractModel):
    source: Literal["derivative"]
    kind: DerivativeKind


class RuleIndicatorParams(ContractModel):
    period: int | None = Field(default=None, strict=True, ge=1, le=MAX_PERIOD)
    anchor: Literal["snapshot", "session"] | None = None
    std_dev: DecimalStr | None = Field(default=None, gt=0)


class RuleIndicator(ContractModel):
    source: Literal["indicator"]
    name: IndicatorName | Literal["legacy_ema", "legacy_atr"]
    version: Literal["1"] = "1"
    channel: str | None = None
    params: RuleIndicatorParams = Field(default_factory=RuleIndicatorParams)

    @property
    def indicator_name(self) -> IndicatorName:
        return IndicatorName(self.name.removeprefix("legacy_").upper())

    @model_validator(mode="after")
    def _validate_operator(self) -> RuleIndicator:
        name = self.indicator_name
        spec_from_params(name, self.params.model_dump(exclude_none=True))
        if self.channel is not None and self.channel not in CHANNELS[name]:
            raise ValueError(f"{name} 不支持通道 {self.channel}")
        return self


RuleOperand = Annotated[
    RuleConstant | RuleBar | RuleEvent | RuleDerivative | RuleIndicator,
    Field(discriminator="source"),
]


class RuleComparison(ContractModel):
    op: Literal["compare"]
    test: Literal["lt", "lte", "gt", "gte", "eq", "cross_above", "cross_below"]
    left: RuleOperand
    right: RuleOperand


class RuleOverlay(ContractModel):
    op: Literal["overlay"]
    drawing_id: str = Field(max_length=64)
    mode: AlertOverlayMode
    direction: AlertDirection
    ratio: DecimalStr | None = Field(default=None, ge=0, le=1)


class RuleLevelEvent(ContractModel):
    op: Literal["level_event"]
    level_id: str = Field(max_length=64)
    event: AlertLevelEvent
    level_kind: KeyLevelKind | None = None


class RuleGroup(ContractModel):
    op: Literal["all", "any", "not"]
    args: list[RuleExpression] = Field(min_length=1, max_length=MAX_RULE_NODES)


RuleExpression = Annotated[
    RuleComparison | RuleOverlay | RuleLevelEvent | RuleGroup,
    Field(discriminator="op"),
]
RuleGroup.model_rebuild()


def _walk(expression: RuleExpression, depth: int = 1) -> Iterator[tuple[RuleExpression, int]]:
    yield expression, depth
    if isinstance(expression, RuleGroup):
        for child in expression.args:
            yield from _walk(child, depth + 1)


class Rule(ContractModel):
    """bar_close 只读确认柱。event 比较必须引用本次行情事件。"""

    version: Literal["1"]
    timing: Literal["bar_close", "event"]
    expression: RuleExpression

    def nodes(self) -> Iterator[RuleExpression]:
        return (node for node, _ in _walk(self.expression))

    @model_validator(mode="after")
    def _validate_timing(self) -> Rule:
        event_driven = self.timing == "event"
        for count, (node, depth) in enumerate(_walk(self.expression), start=1):
            if count > MAX_RULE_NODES or depth > MAX_RULE_DEPTH:
                raise ValueError("规则表达式超出节点数或嵌套深度限制")
            if isinstance(node, RuleGroup) and node.op == "not" and len(node.args) != 1:
                raise ValueError("not 只能有一个子表达式")
            if isinstance(node, RuleComparison):
                operands = (node.left, node.right)
                event_inputs = sum(isinstance(item, RuleEvent) for item in operands)
                if event_inputs != int(event_driven):
                    raise ValueError("事件驱动比较须引用一个当前事件值，收盘比较须使用确认数据")
                if event_driven and (
                    node.test.startswith("cross_")
                    or any(isinstance(item, (RuleBar, RuleDerivative)) for item in operands)
                ):
                    raise ValueError("事件驱动比较只允许当前行情、常量与已收盘的组件输出")
            if event_driven and (
                isinstance(node, RuleOverlay) and node.mode is AlertOverlayMode.CLOSE_BREAK
                or isinstance(node, RuleLevelEvent) and node.event is not AlertLevelEvent.TOUCH
            ):
                raise ValueError("画线突破和关键位突破/回踩需要收盘求值")
        return self
