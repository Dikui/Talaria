"""在冻结输入上运行独立的 AlertEngine，收集观察与实际边沿。"""

from collections.abc import Callable, Mapping, Sequence
from functools import cache

from talaria.components import Ctx
from talaria.components.rules import DEFAULT_RULE_HISTORY, RuleObservation, event_visible_at
from talaria.foundation.alerts import AlertCreate, AlertRecord, AlertStatus, AlertTriggerRecord
from talaria.foundation.bars import CompactBar
from talaria.foundation.derivatives import DerivativeKind, DerivativePoint
from talaria.foundation.drawings import DrawingRecord
from talaria.foundation.feed import DerivativeTickEvent, FeedEvent, KlineEvent, OrderBookEvent
from talaria.foundation.indicators import IndicatorPoint
from talaria.foundation.key_levels import KeyLevel
from talaria.foundation.operators import CHANNELS, pane_for
from talaria.foundation.rules import (
    RulePreviewIndicator, RulePreviewRequest, RulePreviewResponse, RulePreviewSignal,
)
from talaria.foundation.timeframes import timeframe_ms
from talaria.perception.snapshot_events import SnapshotEvent
from talaria.perception.alerts.engine import AlertEngine


def evaluate_preview(
    request: RulePreviewRequest,
    instrument_id: str,
    cutoff_ts: int,
    bars: Sequence[CompactBar],
    read_derivatives: Callable[[], Sequence[DerivativePoint]],
    drawings: Mapping[str, Sequence[DrawingRecord]],
    levels_at: Callable[[int, int, int], Sequence[KeyLevel]],
    *,
    events: Sequence[FeedEvent | SnapshotEvent] | None = None,
    check_cancelled: Callable[[], None] = lambda: None,
) -> RulePreviewResponse:
    rule = request.rule
    interval = timeframe_ms(request.timeframe)
    slots = sorted({bar.timestamp for bar in bars
                    if max(bar.timestamp + interval, bar.ts_available) <= cutoff_ts})[-request.limit:]
    displayed = set(slots)
    lines: dict[str, RulePreviewIndicator] = {}
    for operand in rule.indicators():
        channel = operand.channel or CHANNELS[operand.indicator_name][0]
        operand = operand.model_copy(update={"channel": channel})
        lines.setdefault(operand.model_dump_json(), RulePreviewIndicator(
            operand=operand, pane=pane_for(operand.indicator_name), points=[],
        ))
    values = {key: {} for key in lines}
    conditions: dict[int, RulePreviewSignal] = {}
    triggers = []
    latest_match = None
    receipt = 0

    def observe(ctx: Ctx, observation: RuleObservation | None) -> None:
        nonlocal latest_match
        history = ctx._confirmed_compact
        bar = history[-1] if history else None
        if bar is not None and bar.timestamp in displayed:
            for key, line in lines.items():
                points = ctx._rule_indicator(line.operand, DEFAULT_RULE_HISTORY)
                if points and points[-1].timestamp == bar.timestamp:
                    values[key][bar.timestamp] = points[-1].values[line.operand.channel]
        if observation is None or not observation.matched:
            return
        if rule.timing == "event":
            slot = event.bar.timestamp if isinstance(event, KlineEvent) else getattr(event, "ts_event", receipt) // interval * interval
            if isinstance(event, KlineEvent):
                price = event.bar.close
            elif isinstance(event, OrderBookEvent):
                price = observation.observed if observation.observed in (event.bid_price, event.ask_price) else event.bid_price
            else:
                price = getattr(event, "mark_price", None) or (bar.close if bar else None)
        else:
            slot, price = bar.timestamp, bar.close
        latest_match = None
        if slot in displayed:
            latest_match = RulePreviewSignal(timestamp=slot, price=price, observed_at=max(ctx.now, receipt))
            conditions[slot] = latest_match

    def triggered(record: AlertTriggerRecord, _: AlertRecord) -> None:
        if latest_match is not None:
            triggers.append(latest_match.model_copy(update={"observed_at": record.triggered_ts}))

    if events is None:
        events = [KlineEvent(instrument_id=instrument_id, timeframe=request.timeframe, confirmed=True,
                             revision=0, bar=bar) for bar in bars]
    arrivals = sorted((item if isinstance(item, SnapshotEvent) else SnapshotEvent(0, item) for item in events),
                      key=lambda item: (item.received_at, item.order))
    tick_arrivals = {
        (point.kind, point.ts_event, point.ts_ingested, point.value): (item.received_at, item.order)
        for item in arrivals if isinstance(item.event, DerivativeTickEvent) for point in item.event.points()
    }

    @cache
    def revisions() -> dict[DerivativeKind, list[DerivativePoint]]:
        grouped: dict[DerivativeKind, list[DerivativePoint]] = {kind: [] for kind in DerivativeKind}
        for point in read_derivatives():
            grouped[point.kind].append(point)
        return grouped

    def archive(_: str, kind: DerivativeKind, cutoff: int) -> Sequence[DerivativePoint]:
        points = revisions()[kind]
        if not tick_arrivals:
            return points
        return [p for p in points if p.timeframe is not None or
                tick_arrivals.get((kind, p.ts_event, p.ts_ingested, p.value), (0, 0)) <= (receipt, arrival.order)]

    engine = AlertEngine(
        now_ms=lambda: receipt, key_level_provider=lambda _symbol, _tf, close: levels_at(close, receipt, arrival.order),
        read_archived_derivatives=archive, on_observation=observe, on_trigger=triggered,
    )
    if rule.indicator_snapshot_id:
        engine.seed_chart_window(instrument_id, request.timeframe, rule.indicator_snapshot_id, ())
    config = AlertCreate(instrument_id=instrument_id, timeframe=request.timeframe, condition=rule)
    engine.put(AlertRecord(**config.model_dump(exclude={"draft"}), alert_id="preview",
                           status=AlertStatus.ARMED, created_ts=0, updated_ts=0))
    versions = sorted((version for history in drawings.values() for version in history),
                      key=lambda version: (version.updated_ts, version.version))
    index = 0
    for arrival in arrivals:
        event = arrival.event
        check_cancelled()
        receipt = arrival.received_at
        if receipt > cutoff_ts:
            break
        if event_visible_at(event) > cutoff_ts:
            continue
        while index < len(versions) and versions[index].updated_ts <= receipt:
            engine.refresh_drawings([versions[index]])
            index += 1
        engine.handle_event(event)
    for key, line in lines.items():
        line.points = [IndicatorPoint(timestamp=slot, values={"value": values[key].get(slot)}) for slot in slots]
    return RulePreviewResponse(rule=rule, condition_true=[conditions[slot] for slot in sorted(conditions)],
                               would_trigger=triggers, indicators=list(lines.values()))
