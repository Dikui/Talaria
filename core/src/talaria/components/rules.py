"""将版本化的规则编译到组件接口。行情和引用数据均由宿主传入。"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from operator import eq, ge, gt, le, lt

from talaria.components.contracts import ComponentMeta, SignalEvent
from talaria.components.operators import OPERATORS
from talaria.components.runtime import Ctx
from talaria.foundation.alerts.overlay import overlay_bounds
from talaria.foundation.bars import BarStatus, CompactBar
from talaria.foundation.derivatives import OI_TIMEFRAME_BY_TIMEFRAME, DerivativeKind
from talaria.foundation.drawings import DrawingKind, DrawingRecord
from talaria.foundation.feed import DerivativeTickEvent, FeedEvent, KlineEvent, OrderBookEvent
from talaria.foundation.key_levels import KeyLevel, KeyLevelStatus
from talaria.foundation.operators import CHANNELS
from talaria.foundation.rules import (
    AlertDirection,
    AlertLevelEvent,
    AlertMetric,
    AlertOverlayMode,
    Rule,
    RuleBar,
    RuleComparison,
    RuleConstant,
    RuleDerivative,
    RuleEvent,
    RuleExpression,
    RuleGroup,
    RuleIndicator,
    RuleLevelEvent,
    RuleOperand,
    RuleOverlay,
)
from talaria.foundation.timeframes import timeframe_ms

_COMPARISONS = {
    "lt": lt, "lte": le, "gt": gt, "gte": ge, "eq": eq,
    "cross_above": gt, "cross_below": lt,
}


@dataclass(frozen=True, slots=True)
class RuleObservation:
    matched: bool
    observed: Decimal | None = None
    threshold: Decimal | None = None
    drawing_kind: DrawingKind | None = None
    level: KeyLevel | None = None
    level_event: AlertLevelEvent | None = None
    level_direction: str | None = None
    crossed_at: int | None = None


@dataclass(slots=True)
class RuleState:
    """宿主持有的求值状态，每个回踩节点独立跟踪突破。"""

    breakouts: dict[int, tuple[str, int]] = field(default_factory=dict)
    matched_retests: list[int] = field(default_factory=list)

    def consume_matches(self) -> None:
        for key in self.matched_retests:
            self.breakouts.pop(key, None)
        self.matched_retests.clear()


@dataclass(slots=True)
class _Evaluation:
    ctx: Ctx
    bar: CompactBar | None
    event: FeedEvent | None
    drawing: Callable[[str], DrawingRecord | None] | None
    levels: Mapping[str, KeyLevel]
    state: RuleState

    def operand(self, operand: RuleOperand, *, previous: bool = False) -> Decimal | None:
        ctx = self.ctx
        if isinstance(operand, RuleConstant):
            return operand.value
        if isinstance(operand, RuleEvent):
            return _event_value(operand.metric, self.event)
        if isinstance(operand, RuleDerivative):
            target = {
                DerivativeKind.MARK_PRICE: ctx.timeframe,
                DerivativeKind.FUNDING_RATE: None,
                DerivativeKind.OPEN_INTEREST: OI_TIMEFRAME_BY_TIMEFRAME[ctx.timeframe],
            }[operand.kind]
            cutoff = ctx.now
            if previous and self.bar is not None:
                cutoff = self.bar.timestamp
            return next((
                point.value for point in reversed(ctx.derivatives(operand.kind))
                if point.timeframe == target and point.ts_available <= cutoff
            ), None)
        if isinstance(operand, RuleBar):
            if not previous:
                return getattr(self.bar, operand.field, None)
            return getattr(self._previous_bar(), operand.field, None)
        channel = operand.channel or CHANNELS[operand.indicator_name][0]
        points = ctx.indicator(
            operand.name, version=operand.version, **operand.params.model_dump(exclude_none=True)
        )
        if previous:
            bar = self._previous_bar()
            if bar is None or len(points) < 2 or points[-2].timestamp != bar.timestamp:
                return None
            return points[-2].values.get(channel)
        if not points:
            return None
        return points[-1].values.get(channel)

    def _previous_bar(self) -> CompactBar | None:
        if self.bar is None:
            return None
        bars = self.ctx.compact_bars()
        if len(bars) < 2 or bars[-2].timestamp + timeframe_ms(self.ctx.timeframe) != self.bar.timestamp:
            return None
        return bars[-2]

    def evaluate(self, node: RuleExpression) -> RuleObservation | None:
        if isinstance(node, RuleGroup):
            results = [self.evaluate(child) for child in node.args]
            if node.op == "not":
                result = results[0]
                return None if result is None else RuleObservation(
                    not result.matched, result.observed, result.threshold
                )
            known = [result for result in results if result is not None]
            matched = [result for result in known if result.matched]
            failed = next((result for result in known if not result.matched), None)
            if node.op == "all" and failed is not None:
                return failed
            if matched and (node.op == "any" or len(matched) == len(results)):
                return next((result for result in matched if result.crossed_at is not None), matched[0])
            return failed if len(known) == len(results) else None
        if isinstance(node, RuleComparison):
            return self._compare(node)
        geometry_bar = self.event.bar if isinstance(self.event, KlineEvent) else self.bar
        if geometry_bar is None:
            return None
        if isinstance(node, RuleOverlay):
            return self._overlay(node, geometry_bar)
        return self._level(node, geometry_bar)

    def _compare(self, node: RuleComparison) -> RuleObservation | None:
        left, right = self.operand(node.left), self.operand(node.right)
        if left is None or right is None:
            return None
        test = _COMPARISONS[node.test]
        matched = test(left, right)
        if node.test.startswith("cross_"):
            if self.bar is None or self._previous_bar() is None:
                return None
            previous_left = self.operand(node.left, previous=True)
            previous_right = self.operand(node.right, previous=True)
            if previous_left is None or previous_right is None:
                return None
            matched = matched and not test(previous_left, previous_right)
            return RuleObservation(matched, left, right, crossed_at=self.bar.timestamp if matched else None)
        return RuleObservation(matched, left, right)

    def _overlay(self, node: RuleOverlay, bar: CompactBar) -> RuleObservation | None:
        drawing = self.drawing(node.drawing_id) if self.drawing is not None else None
        if drawing is None or drawing.deleted_at is not None or drawing.instrument_id != self.ctx.instrument:
            return None
        bounds = overlay_bounds(
            drawing, bar.timestamp, ratio=Decimal("0.5") if node.ratio is None else node.ratio,
        )
        if bounds is None:
            return None
        above = node.direction is AlertDirection.ABOVE
        threshold = bounds.upper if above else bounds.lower
        if node.mode is AlertOverlayMode.TOUCH:
            price = bar.high if above else bar.low
            test = ge if above else le
        else:
            price = bar.close
            test = gt if above else lt
        if price is None:
            return None
        return RuleObservation(test(price, threshold), bar.close, threshold, drawing_kind=drawing.kind)

    def _level(self, node: RuleLevelEvent, bar: CompactBar) -> RuleObservation | None:
        level = self.levels.get(node.level_id)
        if level is None or (node.level_kind is not None and level.kind is not node.level_kind):
            return None
        bounds = level_bounds(level)
        if bounds is None or bar.close is None or bar.high is None or bar.low is None:
            return None
        lower, upper = bounds
        close = bar.close
        if node.event is AlertLevelEvent.TOUCH:
            return RuleObservation(
                bar.low <= upper and bar.high >= lower, close, upper if close >= upper else lower,
                level=level, level_event=node.event,
            )

        side = "UP" if close > upper else "DOWN" if close < lower else None
        if side is not None and level.status is KeyLevelStatus.ACTIVE:
            return None
        if node.event is AlertLevelEvent.BREAK:
            return RuleObservation(
                side is not None, close, upper if side == "UP" else lower,
                level=level, level_event=node.event, level_direction=side,
            )

        key = id(node)
        direction, breakout_ts = self.state.breakouts.get(key, (None, None))
        if side is not None and direction != side:
            direction, breakout_ts = self.state.breakouts[key] = (side, bar.timestamp)
        threshold = upper if (direction == "UP" or direction is None and close >= upper) else lower
        matched = False
        if breakout_ts is not None and bar.timestamp > breakout_ts and level.status is not KeyLevelStatus.ACTIVE:
            matched = bar.low <= upper < close if direction == "UP" else close < lower <= bar.high
            if matched:
                self.state.matched_retests.append(key)
        return RuleObservation(
            matched, close, threshold, level=level, level_event=node.event, level_direction=direction,
        )


def _event_value(metric: AlertMetric, event: FeedEvent | None) -> Decimal | None:
    if isinstance(event, KlineEvent) and metric is AlertMetric.LAST_PRICE:
        return event.bar.close
    if isinstance(event, OrderBookEvent) and metric in {AlertMetric.BID_PRICE, AlertMetric.ASK_PRICE}:
        return getattr(event, metric.value)
    if isinstance(event, DerivativeTickEvent) and metric in {
        AlertMetric.MARK_PRICE, AlertMetric.FUNDING_RATE, AlertMetric.OPEN_INTEREST,
    }:
        return getattr(event, metric.value)
    return None


def level_bounds(level: KeyLevel) -> tuple[Decimal, Decimal] | None:
    if level.price is not None:
        return level.price, level.price
    if level.price_low is not None and level.price_high is not None:
        return level.price_low, level.price_high
    return None


@dataclass(frozen=True, slots=True)
class CompiledRule:
    rule: Rule
    meta: ComponentMeta

    def observe(
        self, ctx: Ctx, *, event: FeedEvent | None = None,
        drawing: Callable[[str], DrawingRecord | None] | None = None,
        levels: Mapping[str, KeyLevel] | None = None,
        state: RuleState | None = None,
        consume_retests: bool = True,
    ) -> RuleObservation | None:
        """在盘中事件或确认柱上求值。None 表示输入不可用。"""
        if event is not None and event.instrument_id != ctx.instrument:
            return None
        kline = event if isinstance(event, KlineEvent) else None
        if kline is not None and kline.timeframe != ctx.timeframe:
            return None
        if self.rule.timing == "event":
            if event is None:
                return None
            bar = kline.bar if kline is not None else None
        else:
            if event is not None and (kline is None or not kline.confirmed):
                return None
            bars = ctx.compact_bars()
            if not bars or bars[-1].status is BarStatus.FEED_GAP:
                return None
            bar = bars[-1]
        evaluation = _Evaluation(ctx, bar, event, drawing, levels or {}, state or RuleState())
        evaluation.state.matched_retests.clear()
        result = evaluation.evaluate(self.rule.expression)
        if consume_retests and result is not None and result.matched:
            evaluation.state.consume_matches()
        return result

    def evaluate(self, ctx: Ctx) -> list[SignalEvent]:
        result = self.observe(ctx)
        if result is None or not result.matched:
            return []
        return [SignalEvent(ctx.now, self.meta.name, {"observed": result.observed, "threshold": result.threshold})]


def compile_rule(rule: Rule) -> CompiledRule:
    """解析注册的算子版本，生成可在 Notebook、账户进程与常驻服务复用的信号。"""
    for node in rule.nodes():
        if isinstance(node, RuleComparison):
            for operand in (node.left, node.right):
                if isinstance(operand, RuleIndicator):
                    OPERATORS.get(operand.name, version=operand.version)
    return CompiledRule(rule, ComponentMeta(
        name="rule", version=rule.version, inputs=(rule.timing,), parameters_schema={},
        output_type="signal_events",
    ))
