"""在已冻结的确认柱上预览规则，并保留引用算子的未知值。"""

from collections.abc import Callable, Mapping, Sequence
from functools import cache

from talaria.components import Ctx, RuleState, compile_rule
from talaria.components.rules import DEFAULT_RULE_HISTORY
from talaria.foundation.bars import CompactBar
from talaria.foundation.derivatives import DerivativeKind, DerivativePoint
from talaria.foundation.drawings import DrawingRecord
from talaria.foundation.indicators import IndicatorPoint
from talaria.foundation.key_levels import KeyLevel
from talaria.foundation.operators import CHANNELS, pane_for
from talaria.foundation.rules import (
    AlertLevelEvent, RuleComparison, RuleIndicator, RuleLevelEvent, RulePreviewIndicator, RulePreviewRequest,
    RulePreviewResponse, RulePreviewSignal,
)
from talaria.foundation.timeframes import timeframe_ms


def evaluate_preview(
    request: RulePreviewRequest,
    instrument_id: str,
    cutoff_ts: int,
    bars: Sequence[CompactBar],
    read_derivatives: Callable[[], Sequence[DerivativePoint]],
    drawings: Mapping[str, Sequence[DrawingRecord]],
    levels_at: Callable[[int], Sequence[KeyLevel]],
) -> RulePreviewResponse:
    rule = request.rule
    compiled = compile_rule(rule)
    references: dict[str, RulePreviewIndicator] = {}
    for node in rule.nodes():
        if isinstance(node, RuleComparison):
            for operand in (node.left, node.right):
                if isinstance(operand, RuleIndicator):
                    channel = operand.channel or CHANNELS[operand.indicator_name][0]
                    operand = operand.model_copy(update={"channel": channel})
                    references.setdefault(operand.model_dump_json(), RulePreviewIndicator(
                        operand=operand, pane=pane_for(operand.indicator_name), points=[],
                    ))
    interval = timeframe_ms(request.timeframe)
    @cache
    def derivative_points() -> Sequence[DerivativePoint]:
        return read_derivatives()

    @cache
    def derivatives(kind: DerivativeKind) -> tuple[DerivativePoint, ...]:
        return tuple(point for point in derivative_points() if point.kind is kind)

    observations = [
        (max(bar.timestamp + interval, bar.ts_available), i)
        for i, bar in enumerate(bars)
        if max(bar.timestamp + interval, bar.ts_available) <= cutoff_ts
    ]
    display = observations[-request.limit:]
    first_index = display[0][1] if display else len(bars)
    state = RuleState()
    signals = []

    def drawing_at(drawing_id: str, as_of: int) -> DrawingRecord | None:
        return next(
            (version for version in reversed(drawings.get(drawing_id, ())) if version.updated_ts <= as_of),
            None,
        )

    # 回踩需要窗口之前的状态。普通比较可以直接从首个显示柱开始。
    replay_state = any(
        isinstance(node, RuleLevelEvent) and node.event is AlertLevelEvent.RETEST for node in rule.nodes()
    )
    for as_of, index in sorted(observations if replay_state else display):
        bar = bars[index]
        ctx = Ctx(
            instrument_id, request.timeframe, bars[0].timestamp, as_of,
            _confirmed_compact=bars[:index + 1], _bar_ts=bar.timestamp,
            _read_derivative_revisions=derivatives, _indicator_defaults={},
            drawing=lambda drawing_id: drawing_at(drawing_id, as_of),
            levels={level.level_id: level for level in levels_at(bar.timestamp + interval)},
            rule_state=state,
        )
        # 历史快照不含连续盘中事件。事件规则的已收盘引用线照常计算。
        observation = compiled.observe(ctx) if rule.timing == "bar_close" else None
        if index < first_index:
            continue
        if observation is not None and observation.matched and bar.close is not None:
            signals.append(RulePreviewSignal(timestamp=bar.timestamp, price=bar.close))
        for series in references.values():
            points = ctx._rule_indicator(series.operand, DEFAULT_RULE_HISTORY)
            channel = series.operand.channel
            value = None
            if channel and points and points[-1].timestamp == bar.timestamp:
                value = points[-1].values.get(channel)
            series.points.append(IndicatorPoint(timestamp=bar.timestamp, values={"value": value}))

    signals.sort(key=lambda signal: signal.timestamp)
    for series in references.values():
        series.points.sort(key=lambda point: point.timestamp)
    return RulePreviewResponse(rule=rule, signals=signals, indicators=list(references.values()))
