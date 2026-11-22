"""将版本化的规则编译到组件接口。行情和引用数据均由宿主传入。"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from decimal import Decimal
from operator import eq, ge, gt, le, lt

from talaria.components.contracts import ComponentMeta, SignalEvent
from talaria.components.operators import OPERATORS
from talaria.components.runtime import Ctx, RuleState
from talaria.foundation.alerts.overlay import OverlayBounds, overlay_bounds
from talaria.foundation.bars import BarStatus, CompactBar
from talaria.foundation.derivatives import OI_TIMEFRAME_BY_TIMEFRAME, DerivativeKind
from talaria.foundation.drawings import DrawingKind, DrawingRecord
from talaria.foundation.feed import DerivativeTickEvent, FeedEvent, FeedHealthEvent, KlineEvent, OrderBookEvent
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
    RuleDrawingPrice,
    RuleLevelPrice,
    RuleEvent,
    RuleExpression,
    RuleGroup,
    RuleIndicator,
    RuleLevelEvent,
    RuleOperand,
    RuleOverlay,
)
from talaria.foundation.timeframes import timeframe_ms

# 迁移前的预警窗口。长周期算子的预热和 session VWAP 起点由 Ctx 单独处理。
DEFAULT_RULE_HISTORY = 600

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
            if previous:
                prior = self._previous_bar()
                if prior is None:
                    return None
                cutoff, = ctx._bar_evaluation_times(ctx.timeframe, [prior])
            return next((
                point.value for point in reversed(ctx.derivatives(operand.kind, as_of=cutoff))
                if point.timeframe in (target, None)
                and (operand.kind is not DerivativeKind.FUNDING_RATE or point.is_funding_settlement)
            ), None)
        if isinstance(operand, RuleLevelPrice):
            level = self._visible_level(operand)
            if level is None:
                return None
            bounds = level_bounds(level)
            if bounds is None:
                return None
            return bounds[1] if operand.edge == "upper" else bounds[0]
        if isinstance(operand, RuleDrawingPrice):
            bar = self._previous_bar() if previous else self.bar
            timestamp = bar.timestamp if bar is not None else getattr(self.event, "ts_event", None)
            if timestamp is None:
                return None
            geometry = self._drawing_bounds(operand, timestamp)
            return getattr(geometry[1], operand.edge) if geometry else None
        if isinstance(operand, RuleBar):
            if not previous:
                return getattr(self.bar, operand.field, None)
            return getattr(self._previous_bar(), operand.field, None)
        channel = operand.channel or CHANNELS[operand.indicator_name][0]
        points = ctx._rule_indicator(operand, DEFAULT_RULE_HISTORY)
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
            result = self._compare(node)
        elif self.bar is None:
            return None
        elif isinstance(node, RuleOverlay):
            result = self._overlay(node, self.bar)
        else:
            result = self._level(node, self.bar)
        if result is not None:
            self.state.known_inputs.add(id(node))
        return result

    def _compare(self, node: RuleComparison) -> RuleObservation | None:
        left, right = self.operand(node.left), self.operand(node.right)
        if left is None or right is None:
            return None
        test = _COMPARISONS[node.test]
        matched = test(left, right)
        if node.test.startswith("cross_"):
            if not matched:
                return RuleObservation(False, left, right)
            if self.bar is None or self._previous_bar() is None:
                return None
            previous_left = self.operand(node.left, previous=True)
            previous_right = self.operand(node.right, previous=True)
            if previous_left is None or previous_right is None:
                return None
            matched = matched and not test(previous_left, previous_right)
            return RuleObservation(matched, left, right, crossed_at=self.bar.timestamp if matched else None)
        return RuleObservation(matched, left, right)

    def _drawing_bounds(self, node: RuleOverlay | RuleDrawingPrice, timestamp: int) -> tuple[DrawingRecord, OverlayBounds] | None:
        drawing = self.drawing(node.drawing_id) if self.drawing is not None else None
        if (drawing is None or drawing.deleted_at is not None or drawing.instrument_id != self.ctx.instrument
                or drawing.updated_ts > self.ctx.now):
            return None
        bounds = overlay_bounds(
            drawing, timestamp, ratio=Decimal("0.5") if node.ratio is None else node.ratio,
        )
        return (drawing, bounds) if bounds is not None else None

    def _overlay(self, node: RuleOverlay, bar: CompactBar) -> RuleObservation | None:
        geometry = self._drawing_bounds(node, bar.timestamp)
        if geometry is None:
            return None
        drawing, bounds = geometry
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

    def _visible_level(self, ref: RuleLevelPrice | RuleLevelEvent) -> KeyLevel | None:
        level = self.levels.get(ref.level_id)
        if level is None or level.instrument_id != self.ctx.instrument or level.ts_available > self.ctx.now:
            return None
        if ref.level_kind is not None and level.kind is not ref.level_kind:
            return None
        return level

    def _level(self, node: RuleLevelEvent, bar: CompactBar) -> RuleObservation | None:
        level = self._visible_level(node)
        if level is None:
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
        if self.state.consumed_sides.get(key) != side:
            self.state.consumed_sides.pop(key, None)
        if side is not None and direction != side and key not in self.state.consumed_sides:
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


def event_visible_at(event: FeedEvent) -> int:
    if isinstance(event, KlineEvent):
        received = getattr(event, "ts_ingested", event.bar.timestamp)
        if event.confirmed:
            return max(received, event.bar.ts_available, event.bar.timestamp + timeframe_ms(event.timeframe))
        return max(event.bar.timestamp, received)
    if isinstance(event, FeedHealthEvent):
        return event.ts
    return max(event.ts_event, event.ts_ingested)


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
        event = ctx.event if event is None else event
        state = ctx.rule_state if state is None else state
        drawing = ctx.drawing if drawing is None else drawing
        levels = ctx.levels if levels is None else levels
        state.known_inputs.clear()
        if event is not None and (event.instrument_id != ctx.instrument or event_visible_at(event) > ctx.now):
            return None
        kline = event if isinstance(event, KlineEvent) else None
        if kline is not None and kline.timeframe != ctx.timeframe:
            return None
        if kline is not None:
            slot = kline.bar.timestamp
            if not kline.confirmed:
                slot -= timeframe_ms(ctx.timeframe)
            if ctx._bar_ts is None or ctx._bar_ts > slot:
                ctx = replace(ctx, _bar_ts=slot)
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
            if ctx._bar_ts is not None and bar.timestamp != ctx._bar_ts:
                return None
        evaluation = _Evaluation(ctx, bar, event, drawing, levels, state)
        evaluation.state.matched_retests.clear()
        result = evaluation.evaluate(self.rule.expression)
        if consume_retests and result is not None and result.matched:
            evaluation.state.consume_matches()
        return result

    def prior_root_false(self, ctx: Ctx) -> bool:
        """显式穿越可用上一根确认柱证明首次观察前的根条件。"""
        bars = ctx.compact_bars()
        if len(bars) < 2:
            return False
        prior = bars[-2]
        previous = replace(ctx, now=max(prior.ts_available, prior.timestamp + timeframe_ms(ctx.timeframe)),
                           _bar_ts=prior.timestamp, event=None, rule_state=RuleState())
        result = self.observe(previous)
        return result is not None and not result.matched

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
