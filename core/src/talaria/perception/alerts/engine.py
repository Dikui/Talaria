"""预警引擎：消费 ``MarketEventFeed`` 事件，维护规则状态并产出触发。

引擎是纯逻辑组件：时钟、历史柱与证据快照冻结都通过回调注入，因此实时流、
回放流与离线测试共享同一套触发与冷却代码。前端不做任何指标或几何计算。
"""

from __future__ import annotations

import logging
from bisect import bisect_left
from collections import deque
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from uuid import uuid4

from talaria.foundation.alerts.contracts import (
    AlertConditionKind,
    AlertDirection,
    AlertLevelEvent,
    AlertMetric,
    AlertOverlayMode,
    AlertRecord,
    AlertStatus,
    AlertTriggerRecord,
)
from talaria.foundation.alerts.overlay import OverlayBounds, overlay_bounds
from talaria.foundation.bars import BarStatus, CompactBar
from talaria.foundation.drawings import DrawingKind, DrawingRecord
from talaria.foundation.feed import (
    DerivativeTickEvent,
    FeedEvent,
    FeedHealthEvent,
    FeedHealthStatus,
    KlineEvent,
    OrderBookEvent,
)
from talaria.foundation.key_levels import (
    KeyLevel,
    KeyLevelStatus,
    compute_key_levels,
)
from talaria.foundation.operators import (
    CHANNELS,
    IndicatorName,
    IndicatorSpec,
    Point,
    compute_series,
    spec_from_params,
)

logger = logging.getLogger(__name__)

_METRIC_LABELS = {
    AlertMetric.LAST_PRICE: "价格",
    AlertMetric.BID_PRICE: "买一价",
    AlertMetric.ASK_PRICE: "卖一价",
    AlertMetric.MARK_PRICE: "标记价格",
    AlertMetric.FUNDING_RATE: "资金费率",
    AlertMetric.OPEN_INTEREST: "持仓量",
}

_OVERLAY_LABELS = {
    DrawingKind.HORIZONTAL_LINE: "水平线",
    DrawingKind.RAY: "射线",
    DrawingKind.SEGMENT: "线段",
    DrawingKind.PARALLEL_CHANNEL: "通道",
    DrawingKind.FIB_RETRACEMENT: "斐波那契",
    DrawingKind.RECTANGLE: "矩形",
}

_THICK_OVERLAYS = frozenset({DrawingKind.PARALLEL_CHANNEL, DrawingKind.RECTANGLE})

#: 关键位来源：instrument、timeframe、level_id、cutoff → 该 cutoff 已可见的最新关键位。
KeyLevelProvider = Callable[[str, str, str, int], KeyLevel | None]

_LEVEL_EVENT_LABELS = {
    AlertLevelEvent.TOUCH: "触及",
    AlertLevelEvent.BREAK: "收盘突破",
    AlertLevelEvent.RETEST: "回踩确认",
}


@dataclass(slots=True)
class _Runtime:
    """单条规则的求值状态；不持久化，重启后从最近柱重建基线。"""

    condition_true: bool | None = None
    baseline_pending: bool = True
    last_bar_key: tuple[int, int] | None = None
    level_status: KeyLevelStatus | None = None
    level_breakout: str | None = None
    level_breakout_ts: int | None = None


@dataclass(slots=True)
class _Observation:
    value: Decimal | None
    observed: Decimal | None
    threshold: Decimal | None
    bar: CompactBar | None = None
    bounds: OverlayBounds | None = None
    drawing_kind: DrawingKind | None = None
    level: KeyLevel | None = None
    level_event: AlertLevelEvent | None = None
    level_direction: str | None = None


class AlertEngine:
    """事件驱动的预警状态机。"""

    def __init__(
        self,
        *,
        now_ms: Callable[[], int],
        overlay_geometry: Callable[[str], DrawingRecord | None] | None = None,
        history_provider: Callable[[str, str], Sequence[CompactBar]] | None = None,
        key_level_provider: KeyLevelProvider | None = None,
        freeze_snapshot: Callable[[AlertRecord, int], str] | None = None,
        on_trigger: Callable[[AlertTriggerRecord, AlertRecord], None] | None = None,
        bar_limit: int = 600,
    ) -> None:
        self._now_ms = now_ms
        self._overlay_geometry = overlay_geometry
        self._history_provider = history_provider
        self._key_level_provider = key_level_provider
        self._level_cache: dict[tuple[str, str, int], list[KeyLevel]] = {}
        self._freeze_snapshot = freeze_snapshot
        self._on_trigger = on_trigger
        self._bar_limit = max(bar_limit, 32)
        self._alerts: dict[str, AlertRecord] = {}
        self._runtimes: dict[str, _Runtime] = {}
        self._windows: dict[tuple[str, str], deque[CompactBar]] = {}
        self._seeded: set[tuple[str, str]] = set()
        self._drawings: dict[str, DrawingRecord] = {}
        self._stale: set[str] = set()
        self._skip_through: dict[str, int] = {}

    # ------------------------------------------------------------------ 规则集

    def load(self, alerts: Iterable[AlertRecord]) -> None:
        for alert in alerts:
            self.put(alert, reset=True)

    def put(self, alert: AlertRecord, *, reset: bool = False) -> None:
        previous = self._alerts.get(alert.alert_id)
        self._alerts[alert.alert_id] = alert
        if reset or previous is None:
            self._runtimes[alert.alert_id] = _Runtime()
        self._ensure_window(alert.instrument_id, alert.timeframe)
        self._warm_drawing(alert.condition.drawing_id)

    def remove(self, alert_id: str) -> None:
        self._alerts.pop(alert_id, None)
        self._runtimes.pop(alert_id, None)

    def get(self, alert_id: str) -> AlertRecord | None:
        return self._alerts.get(alert_id)

    def list_alerts(self) -> list[AlertRecord]:
        return sorted(self._alerts.values(), key=lambda alert: (alert.created_ts, alert.alert_id))

    def active_instrument_ids(self) -> tuple[str, ...]:
        """当前 ARMED 的标的；冷却/过期/草案都不占用订阅优先级。"""

        ordered = sorted(
            (alert for alert in self._alerts.values() if alert.status is AlertStatus.ARMED),
            key=lambda alert: (alert.created_ts, alert.alert_id),
        )
        return tuple(dict.fromkeys(alert.instrument_id for alert in ordered))

    def overlay_alerts(self) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                alert.condition.drawing_id
                for alert in self._alerts.values()
                if alert.condition.drawing_id is not None
            )
        )

    def refresh_drawings(self, drawings: Iterable[DrawingRecord]) -> None:
        for drawing in drawings:
            if drawing.deleted_at is not None:
                self._drawings.pop(drawing.drawing_id, None)
            else:
                self._drawings[drawing.drawing_id] = drawing

    def drop_drawings(self, drawing_ids: Iterable[str]) -> None:
        for drawing_id in drawing_ids:
            self._drawings.pop(drawing_id, None)

    def seed_bars(
        self, instrument_id: str, timeframe: str, bars: Sequence[CompactBar]
    ) -> None:
        key = (instrument_id, timeframe)
        window = self._windows.setdefault(key, deque(maxlen=self._bar_limit))
        for bar in bars:
            self._merge_bar(window, bar)
        self._seeded.add(key)

    # -------------------------------------------------------------- 事件消费

    def handle_event(self, event: FeedEvent) -> list[AlertRecord]:
        """消费一个行情/健康事件，返回需要持久化的规则状态变化。"""

        if isinstance(event, FeedHealthEvent):
            self._handle_health(event)
            return []
        self._store_event_bars(event)
        self._level_cache.clear()
        instrument_id = event.instrument_id
        if instrument_id in self._stale:
            return []
        now = self._now_ms()
        changed: dict[str, AlertRecord] = {}
        for alert in self.list_alerts():
            if alert.instrument_id != instrument_id:
                continue
            if self._advance(alert, now):
                changed[alert.alert_id] = alert
            if alert.status is not AlertStatus.ARMED:
                continue
            if isinstance(event, KlineEvent) and event.bar.timestamp <= self._skip_through.get(
                instrument_id, -1
            ):
                continue
            if self._evaluate(alert, event, now):
                # 触发后的规则由回调持久化，避免同一事件写两次。
                changed.pop(alert.alert_id, None)
        return list(changed.values())

    def tick(self, now: int | None = None) -> list[AlertRecord]:
        """推进冷却/过期；返回需要持久化的规则。"""

        current = self._now_ms() if now is None else now
        changed: list[AlertRecord] = []
        for alert in self.list_alerts():
            if self._advance(alert, current):
                changed.append(alert)
        return changed

    # ----------------------------------------------------------------- 内部

    @staticmethod
    def _merge_bar(window: deque[CompactBar], bar: CompactBar) -> None:
        timestamps = [item.timestamp for item in window]
        index = bisect_left(timestamps, bar.timestamp)
        if index < len(window) and window[index].timestamp == bar.timestamp:
            window[index] = bar
        elif index == len(window):
            window.append(bar)
        else:
            window.insert(index, bar)

    def _ensure_window(self, instrument_id: str, timeframe: str) -> None:
        key = (instrument_id, timeframe)
        if key in self._seeded or self._history_provider is None:
            self._windows.setdefault(key, deque(maxlen=self._bar_limit))
            return
        self._seeded.add(key)
        window = self._windows.setdefault(key, deque(maxlen=self._bar_limit))
        try:
            bars = self._history_provider(instrument_id, timeframe)
        except Exception:
            logger.exception("载入预警历史柱失败：%s %s", instrument_id, timeframe)
            return
        for bar in bars:
            self._merge_bar(window, bar)

    def _warm_drawing(self, drawing_id: str | None) -> None:
        if drawing_id is None or drawing_id in self._drawings:
            return
        if self._overlay_geometry is None:
            return
        try:
            drawing = self._overlay_geometry(drawing_id)
        except Exception:
            logger.exception("载入画线几何失败：%s", drawing_id)
            return
        if drawing is not None:
            self.refresh_drawings([drawing])

    def _drawing_for(self, drawing_id: str) -> DrawingRecord | None:
        drawing = self._drawings.get(drawing_id)
        if drawing is None:
            self._warm_drawing(drawing_id)
            drawing = self._drawings.get(drawing_id)
        return drawing

    def _handle_health(self, event: FeedHealthEvent) -> None:
        if event.status is FeedHealthStatus.LIVE:
            if event.instrument_id in self._stale:
                self._stale.discard(event.instrument_id)
                if event.last_event_ts is not None:
                    self._skip_through[event.instrument_id] = event.last_event_ts
                for alert in self.list_alerts():
                    if alert.instrument_id == event.instrument_id:
                        runtime = self._runtime_for(alert)
                        runtime.baseline_pending = True
                        runtime.condition_true = None
                        runtime.level_breakout = None
                        runtime.level_breakout_ts = None
            return
        self._stale.add(event.instrument_id)

    def _store_event_bars(self, event: FeedEvent) -> None:
        if not isinstance(event, KlineEvent):
            return
        key = (event.instrument_id, event.timeframe)
        window = self._windows.get(key)
        if window is None:
            return
        self._merge_bar(window, event.bar)

    def _runtime_for(self, alert: AlertRecord) -> _Runtime:
        runtime = self._runtimes.get(alert.alert_id)
        if runtime is None:
            runtime = _Runtime()
            self._runtimes[alert.alert_id] = runtime
        return runtime

    def _advance(self, alert: AlertRecord, now: int) -> bool:
        if alert.status in {AlertStatus.DRAFT, AlertStatus.DISABLED, AlertStatus.EXPIRED}:
            return False
        changed = False
        if alert.expires_at is not None and now >= alert.expires_at:
            alert.status = AlertStatus.EXPIRED
            alert.next_arm_ts = None
            changed = True
        elif (
            alert.status is AlertStatus.COOLDOWN
            and alert.next_arm_ts is not None
            and now >= alert.next_arm_ts
        ):
            alert.status = AlertStatus.ARMED
            alert.next_arm_ts = None
            runtime = self._runtime_for(alert)
            runtime.baseline_pending = True
            runtime.condition_true = None
            runtime.level_breakout = None
            runtime.level_breakout_ts = None
            changed = True
        if changed:
            alert.updated_ts = now
        return changed

    def _evaluate(self, alert: AlertRecord, event: FeedEvent, now: int) -> bool:
        condition = alert.condition
        runtime = self._runtime_for(alert)

        if isinstance(event, KlineEvent) and event.timeframe != alert.timeframe:
            return False

        if isinstance(event, KlineEvent):
            bar_key = (event.bar.timestamp, event.revision)
            if runtime.last_bar_key == bar_key:
                return False

        observation = self._observe(alert, event)
        if observation is None or observation.value is None:
            return False
        if isinstance(event, KlineEvent):
            runtime.last_bar_key = (event.bar.timestamp, event.revision)

        if runtime.baseline_pending:
            runtime.condition_true = observation.value
            runtime.baseline_pending = False
            return False

        previous = runtime.condition_true
        runtime.condition_true = observation.value
        if not observation.value or previous is not False:
            return False

        self._trigger(alert, observation, now)
        if (
            condition.kind is AlertConditionKind.LEVEL_EVENT
            and condition.level_event is AlertLevelEvent.RETEST
        ):
            runtime.level_breakout = None
            runtime.level_breakout_ts = None
        return True

    def _observe(self, alert: AlertRecord, event: FeedEvent) -> _Observation | None:
        condition = alert.condition
        if condition.kind is AlertConditionKind.METRIC_THRESHOLD:
            value = self._metric_value(condition.metric, event)
            if value is None or condition.threshold is None:
                return None
            comparison = (
                value >= condition.threshold
                if condition.direction is AlertDirection.ABOVE
                else value <= condition.threshold
            )
            return _Observation(
                value=comparison,
                observed=value,
                threshold=condition.threshold,
            )

        if condition.kind is AlertConditionKind.INDICATOR_SIGNAL:
            if not isinstance(event, KlineEvent) or not event.confirmed:
                return None
            return self._observe_indicator(alert, event)

        if condition.kind is AlertConditionKind.OVERLAY_CROSS:
            if not isinstance(event, KlineEvent):
                return None
            if condition.overlay_mode is AlertOverlayMode.CLOSE_BREAK and not event.confirmed:
                return None
            drawing = (
                self._drawing_for(condition.drawing_id)
                if condition.drawing_id is not None
                else None
            )
            if drawing is None:
                return None
            bounds = overlay_bounds(
                drawing,
                event.bar.timestamp,
                ratio=(
                    Decimal("0.5")
                    if condition.overlay_ratio is None
                    else condition.overlay_ratio
                ),
            )
            if bounds is None:
                return None
            level = bounds.upper if condition.direction is AlertDirection.ABOVE else bounds.lower
            bar = event.bar
            if condition.overlay_mode is AlertOverlayMode.TOUCH:
                extreme = bar.high if condition.direction is AlertDirection.ABOVE else bar.low
                if extreme is None:
                    return None
                comparison = extreme >= level if condition.direction is AlertDirection.ABOVE else extreme <= level
            else:
                if bar.close is None or bar.status is BarStatus.FEED_GAP:
                    return None
                comparison = (
                    bar.close > level
                    if condition.direction is AlertDirection.ABOVE
                    else bar.close < level
                )
            return _Observation(
                value=comparison,
                observed=bar.close,
                threshold=level,
                bar=bar,
                bounds=bounds,
                drawing_kind=drawing.kind,
            )

        if condition.kind is AlertConditionKind.LEVEL_EVENT:
            if not isinstance(event, KlineEvent):
                return None
            return self._observe_level(alert, event)

        return None

    def _observe_level(self, alert: AlertRecord, event: KlineEvent) -> _Observation | None:
        """按 ``level_id`` 读取 cutoff 前最新状态，判定触及/突破/回踩。"""

        condition = alert.condition
        level_event = condition.level_event
        level_id = condition.level_id
        if level_event is None or level_id is None:
            return None
        bar = event.bar
        if level_event is not AlertLevelEvent.TOUCH and not event.confirmed:
            return None
        if bar.close is None or bar.high is None or bar.low is None:
            return None
        cutoff_ts = bar.ts_available if event.confirmed else bar.timestamp
        level = self._key_level(alert.instrument_id, alert.timeframe, level_id, cutoff_ts)
        if level is None:
            return None
        if condition.level_kind is not None and level.kind is not condition.level_kind:
            return None
        bounds = _level_bounds(level)
        if bounds is None:
            return None
        lower, upper = bounds
        runtime = self._runtime_for(alert)
        runtime.level_status = level.status

        if level_event is AlertLevelEvent.TOUCH:
            touched = bar.low <= upper and bar.high >= lower
            return _Observation(
                value=touched,
                observed=bar.close,
                threshold=upper if bar.close >= upper else lower,
                bar=bar,
                level=level,
                level_event=level_event,
            )

        if bar.status is BarStatus.FEED_GAP:
            return None
        close = bar.close
        side = "UP" if close > upper else "DOWN" if close < lower else None
        if side is not None and level.status is KeyLevelStatus.ACTIVE:
            # 收盘已越过，但关键位仍报 ACTIVE：以最新状态为准，本次不判定。
            return None

        if level_event is AlertLevelEvent.BREAK:
            return _Observation(
                value=side is not None,
                observed=close,
                threshold=upper if side == "UP" else lower,
                bar=bar,
                level=level,
                level_event=level_event,
                level_direction=side,
            )

        # RETEST：记录突破方向与时刻；FLIPPED 表示价格已回到另一侧，方向随之翻转。
        if side is not None and runtime.level_breakout != side:
            runtime.level_breakout = side
            runtime.level_breakout_ts = bar.timestamp
        direction = runtime.level_breakout
        if direction is None or runtime.level_breakout_ts is None:
            return _Observation(
                value=False,
                observed=close,
                threshold=upper if close >= upper else lower,
                bar=bar,
                level=level,
                level_event=level_event,
            )
        if bar.timestamp <= runtime.level_breakout_ts or level.status is KeyLevelStatus.ACTIVE:
            return _Observation(
                value=False,
                observed=close,
                threshold=upper if direction == "UP" else lower,
                bar=bar,
                level=level,
                level_event=level_event,
                level_direction=direction,
            )
        if direction == "UP":
            retested = bar.low <= upper and close > upper
            threshold = upper
        else:
            retested = bar.high >= lower and close < lower
            threshold = lower
        return _Observation(
            value=retested,
            observed=close,
            threshold=threshold,
            bar=bar,
            level=level,
            level_event=level_event,
            level_direction=direction,
        )

    def _key_level(
        self, instrument_id: str, timeframe: str, level_id: str, cutoff_ts: int
    ) -> KeyLevel | None:
        provider = self._key_level_provider
        if provider is not None:
            return provider(instrument_id, timeframe, level_id, cutoff_ts)
        cache_key = (instrument_id, timeframe, cutoff_ts)
        cached = self._level_cache.get(cache_key)
        if cached is None:
            window = self._windows.get((instrument_id, timeframe))
            if not window:
                return None
            try:
                cached = compute_key_levels(
                    instrument_id=instrument_id,
                    timeframe=timeframe,
                    bars=list(window),
                    cutoff_ts=cutoff_ts,
                )
            except Exception:
                logger.exception("重算关键位失败：%s %s", instrument_id, timeframe)
                return None
            self._level_cache[cache_key] = cached
        return next((level for level in cached if level.level_id == level_id), None)

    @staticmethod
    def _metric_value(metric: AlertMetric | None, event: FeedEvent) -> Decimal | None:
        if metric is AlertMetric.LAST_PRICE and isinstance(event, KlineEvent):
            return event.bar.close
        if isinstance(event, OrderBookEvent):
            if metric is AlertMetric.BID_PRICE:
                return event.bid_price
            if metric is AlertMetric.ASK_PRICE:
                return event.ask_price
            return None
        if isinstance(event, DerivativeTickEvent):
            if metric is AlertMetric.MARK_PRICE:
                return event.mark_price
            if metric is AlertMetric.FUNDING_RATE:
                return event.funding_rate
            if metric is AlertMetric.OPEN_INTEREST:
                return event.open_interest
        return None

    def _observe_indicator(self, alert: AlertRecord, event: KlineEvent) -> _Observation | None:
        condition = alert.condition
        assert condition.indicator is not None
        window = self._windows.get((alert.instrument_id, alert.timeframe))
        if not window:
            return None
        bars = list(window)
        spec = self._indicator_spec(
            condition.indicator,
            condition.indicator_period,
            condition.indicator_anchor,
        )
        channel = condition.indicator_channel or CHANNELS[condition.indicator][0]
        points = list(compute_series(bars, spec).points)
        if not points:
            return None
        current = points[-1].get(channel)
        if current is None:
            return None
        if condition.compare_indicator is not None:
            compare_spec = self._indicator_spec(
                condition.compare_indicator,
                condition.compare_period,
                condition.compare_anchor,
            )
            compare_channel = CHANNELS[condition.compare_indicator][0]
            compare_points = list(compute_series(bars, compare_spec).points)
            compare = compare_points[-1].get(compare_channel)
            if compare is None:
                return None
            comparison = (
                current >= compare
                if condition.direction is AlertDirection.ABOVE
                else current <= compare
            )
            return _Observation(value=comparison, observed=current, threshold=None)
        if condition.threshold is None:
            return None
        comparison = (
            current >= condition.threshold
            if condition.direction is AlertDirection.ABOVE
            else current <= condition.threshold
        )
        return _Observation(
            value=comparison,
            observed=current,
            threshold=condition.threshold,
        )

    @staticmethod
    def _indicator_spec(
        name: IndicatorName, period: int | None, anchor: str | None = None
    ) -> IndicatorSpec:
        if period is None:
            raise ValueError(f"{name} 预警缺少显式周期")
        params: dict[str, object] = {"period": period}
        if name is IndicatorName.VWAP:
            params["anchor"] = anchor or "session"
        return spec_from_params(name, params)

    def _trigger(self, alert: AlertRecord, observation: _Observation, now: int) -> None:
        condition = alert.condition
        snapshot_id = ""
        if self._freeze_snapshot is not None:
            try:
                snapshot_id = self._freeze_snapshot(alert, now)
            except Exception:
                logger.exception("冻结触发证据快照失败：%s", alert.alert_id)
        trigger = AlertTriggerRecord(
            trigger_id=f"trg_{uuid4().hex}",
            alert_id=alert.alert_id,
            instrument_id=alert.instrument_id,
            timeframe=alert.timeframe,
            condition_kind=condition.kind,
            triggered_ts=now,
            snapshot_id=snapshot_id,
            observed=observation.observed,
            threshold=observation.threshold,
            message=self._message(alert, observation),
            note=alert.note,
        )
        alert.trigger_count += 1
        alert.last_triggered_ts = now
        alert.updated_ts = now
        if not alert.repeat:
            alert.status = AlertStatus.EXPIRED
            alert.next_arm_ts = None
        elif alert.cooldown_ms > 0:
            alert.status = AlertStatus.COOLDOWN
            alert.next_arm_ts = now + alert.cooldown_ms
        else:
            alert.status = AlertStatus.ARMED
            alert.next_arm_ts = None
        if self._on_trigger is not None:
            try:
                self._on_trigger(trigger, alert)
            except Exception:
                logger.exception("预警触发回调失败：%s", alert.alert_id)

    @staticmethod
    def _message(alert: AlertRecord, observation: _Observation) -> str:
        condition = alert.condition
        if condition.kind is AlertConditionKind.LEVEL_EVENT:
            return _level_message(observation)
        direction = "上穿" if condition.direction is AlertDirection.ABOVE else "下穿"
        if condition.kind is AlertConditionKind.METRIC_THRESHOLD:
            label = _METRIC_LABELS.get(condition.metric, "指标")
            return f"{label}{direction} {_decimal_text(observation.threshold)}"
        if condition.kind is AlertConditionKind.INDICATOR_SIGNAL:
            assert condition.indicator is not None
            period = condition.indicator_period
            assert period is not None
            if condition.compare_indicator is not None:
                compare_period = condition.compare_period
                assert compare_period is not None
                return (
                    f"{condition.indicator.value}({period}) {direction} "
                    f"{condition.compare_indicator.value}({compare_period})"
                )
            return (
                f"{condition.indicator.value}({period}) {direction} "
                f"{_decimal_text(observation.threshold)}"
            )
        drawing_kind = observation.drawing_kind
        label = _OVERLAY_LABELS.get(drawing_kind, "画线") if drawing_kind else "画线"
        edge = ""
        if drawing_kind in _THICK_OVERLAYS:
            edge = "上沿" if condition.direction is AlertDirection.ABOVE else "下沿"
        action = (
            "收盘突破"
            if condition.overlay_mode is AlertOverlayMode.CLOSE_BREAK
            else "触碰"
        )
        return f"价格{action}{label}{edge} {_decimal_text(observation.threshold)}"


def _level_bounds(level: KeyLevel) -> tuple[Decimal, Decimal] | None:
    if level.price is not None:
        return level.price, level.price
    if level.price_low is not None and level.price_high is not None:
        return level.price_low, level.price_high
    return None


def _level_message(observation: _Observation) -> str:
    level = observation.level
    bounds = _level_bounds(level) if level is not None else None
    label = "-"
    if bounds is not None:
        label = (
            _decimal_text(bounds[0])
            if bounds[0] == bounds[1]
            else f"{_decimal_text(bounds[0])}–{_decimal_text(bounds[1])}"
        )
    kind = level.kind.value if level is not None else "-"
    status = level.status.value if level is not None else "-"
    action = _LEVEL_EVENT_LABELS.get(observation.level_event, "关键位事件")
    if observation.level_event is AlertLevelEvent.TOUCH:
        return f"价格触及关键位 {label}（{kind}）"
    if observation.level_event is AlertLevelEvent.BREAK:
        direction = "上穿" if observation.level_direction == "UP" else "下穿"
        return f"收盘{direction}关键位 {label}（{kind}·{status}）"
    side = "上方" if observation.level_direction == "UP" else "下方"
    return f"价格回踩关键位 {label} 并收盘回到{side}（{kind}·{status}）"


def _decimal_text(value: Decimal | None) -> str:
    if value is None:
        return "-"
    return format(value.normalize(), "f")