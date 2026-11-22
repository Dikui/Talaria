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

from talaria.components import CompiledRule, Ctx, compile_rule
from talaria.components.rules import RuleObservation, RuleState, level_bounds
from talaria.foundation.alerts.contracts import AlertRecord, AlertStatus, AlertTriggerKind, AlertTriggerRecord
from talaria.foundation.bars import CompactBar
from talaria.foundation.derivatives import DerivativeKind, DerivativePoint, OI_TIMEFRAME_BY_TIMEFRAME
from talaria.foundation.drawings import DrawingKind, DrawingRecord
from talaria.foundation.feed import (
    DerivativeTickEvent,
    FeedEvent,
    FeedHealthEvent,
    FeedHealthStatus,
    KlineEvent,
    ReceivedKlineEvent,
)
from talaria.foundation.key_levels import KeyLevel, compute_key_levels
from talaria.foundation.operators import spec_from_params
from talaria.foundation.rules import (
    AlertDirection, AlertLevelEvent, AlertMetric, AlertOverlayMode,
    RuleComparison, RuleConstant, RuleEvent, RuleIndicator, RuleLevelEvent, RuleOverlay,
)
from talaria.foundation.timeframes import TIMEFRAME_MS

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

#: 关键位来源：instrument、timeframe、cutoff → 该 cutoff 已可见的一版关键位。
KeyLevelProvider = Callable[[str, str, int], Sequence[KeyLevel]]

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
    last_cross_ts: int | None = None
    rule_state: RuleState = field(default_factory=RuleState)


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
        self._freeze_snapshot = freeze_snapshot
        self._on_trigger = on_trigger
        self._bar_limit = max(bar_limit, 32)
        self._compiled: dict[str, CompiledRule] = {}
        self._derivatives: dict[str, deque[DerivativeTickEvent]] = {}
        self._alerts: dict[str, AlertRecord] = {}
        self._runtimes: dict[str, _Runtime] = {}
        self._windows: dict[tuple[str, str], deque[CompactBar]] = {}
        self._session_history: dict[tuple[str, str], tuple[CompactBar, ...]] = {}
        self._seeded: set[tuple[str, str]] = set()
        self._drawings: dict[str, DrawingRecord] = {}
        self._stale: set[tuple[str, str]] = set()
        self._skip_through: dict[tuple[str, str], int] = {}

    # ------------------------------------------------------------------ 规则集

    def load(self, alerts: Iterable[AlertRecord]) -> None:
        for alert in alerts:
            self.put(alert, reset=True)

    def put(self, alert: AlertRecord, *, reset: bool = False) -> None:
        previous = self._alerts.get(alert.alert_id)
        if reset or previous is None or previous.condition != alert.condition:
            self._compiled[alert.alert_id] = compile_rule(alert.condition)
            self._runtimes[alert.alert_id] = _Runtime()
        self._alerts[alert.alert_id] = alert
        self._ensure_window(alert.instrument_id, alert.timeframe)
        for node in alert.condition.nodes():
            if isinstance(node, RuleOverlay):
                self._warm_drawing(node.drawing_id)

    def remove(self, alert_id: str) -> None:
        self._alerts.pop(alert_id, None)
        self._runtimes.pop(alert_id, None)
        self._compiled.pop(alert_id, None)

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
                node.drawing_id
                for alert in self._alerts.values()
                for node in alert.condition.nodes()
                if isinstance(node, RuleOverlay)
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
        window = self._window_for(key)
        for bar in bars:
            self._merge_bar(window, bar)
        self._seeded.add(key)

    def seed_session_bars(
        self, instrument_id: str, timeframe: str, bars: Sequence[CompactBar]
    ) -> None:
        """Supply closed bars from before the realtime subscription began."""
        self._session_history[(instrument_id, timeframe)] = tuple(bars)

    # -------------------------------------------------------------- 事件消费

    def handle_event(self, event: FeedEvent) -> list[AlertRecord]:
        """消费一个行情/健康事件，返回需要持久化的规则状态变化。"""

        if isinstance(event, FeedHealthEvent):
            self._handle_health(event)
            return []
        if isinstance(event, DerivativeTickEvent):
            self._derivatives.setdefault(event.instrument_id, deque(maxlen=self._bar_limit)).append(event)
        self._store_event_bars(event)
        instrument_id = event.instrument_id
        now = self._now_ms()
        alerts = [
            alert for alert in self.list_alerts()
            if alert.instrument_id == instrument_id and (instrument_id, alert.timeframe) not in self._stale
        ]
        levels: dict[str, KeyLevel] = {}
        if isinstance(event, KlineEvent) and any(
            alert.timeframe == event.timeframe
            and any(isinstance(node, RuleLevelEvent) for node in alert.condition.nodes())
            and alert.status in {AlertStatus.ARMED, AlertStatus.COOLDOWN}
            for alert in alerts
        ):
            levels = {level.level_id: level for level in self._levels_for_event(event, now)}
        changed: dict[str, AlertRecord] = {}
        for alert in alerts:
            if self._advance(alert, now):
                changed[alert.alert_id] = alert
            if alert.status is not AlertStatus.ARMED:
                continue
            if isinstance(event, KlineEvent) and event.bar.timestamp <= self._skip_through.get(
                (instrument_id, event.timeframe), -1
            ):
                continue
            if self._evaluate(alert, event, now, levels):
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
        if not window or bar.timestamp > window[-1].timestamp:
            window.append(bar)
            return
        timestamps = [item.timestamp for item in window]
        index = bisect_left(timestamps, bar.timestamp)
        if index < len(window) and window[index].timestamp == bar.timestamp:
            window[index] = bar
        else:
            if len(window) == window.maxlen:
                if index == 0:
                    return
                window.popleft()
                index -= 1
            window.insert(index, bar)

    def _window_for(self, key: tuple[str, str]) -> deque[CompactBar]:
        # A UTC day of aligned bars and one preceding bar expose the first anchor.
        day_ms = TIMEFRAME_MS["1d"]
        session_size = day_ms // TIMEFRAME_MS.get(key[1], day_ms) + 1
        return self._windows.setdefault(
            key, deque(maxlen=max(self._bar_limit, session_size))
        )

    def _ensure_window(self, instrument_id: str, timeframe: str) -> None:
        key = (instrument_id, timeframe)
        window = self._window_for(key)
        if key in self._seeded or self._history_provider is None:
            return
        self._seeded.add(key)
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

    def suspend_channel(self, key: tuple[str, str]) -> None:
        """配额退订或行情断流后，等待 LIVE 并重新建立求值基线。"""
        self._stale.add(key)

    def _handle_health(self, event: FeedHealthEvent) -> None:
        key = (event.instrument_id, event.timeframe)
        if event.status is FeedHealthStatus.LIVE:
            if key in self._stale:
                self._stale.discard(key)
                if event.last_event_ts is not None:
                    self._skip_through[key] = event.last_event_ts
                for alert in self.list_alerts():
                    if (alert.instrument_id, alert.timeframe) == key:
                        runtime = self._runtime_for(alert)
                        runtime.baseline_pending = True
                        runtime.condition_true = None
                        runtime.rule_state = RuleState()
            return
        self.suspend_channel(key)

    def _store_event_bars(self, event: FeedEvent) -> None:
        if not isinstance(event, KlineEvent) or not event.confirmed:
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
            runtime.rule_state = RuleState()
            changed = True
        if changed:
            alert.updated_ts = now
        return changed

    def _evaluate(
        self, alert: AlertRecord, event: FeedEvent, now: int, levels: dict[str, KeyLevel]
    ) -> bool:
        runtime = self._runtime_for(alert)

        if isinstance(event, KlineEvent) and event.timeframe != alert.timeframe:
            return False

        if isinstance(event, KlineEvent):
            bar_key = (event.bar.timestamp, event.revision)
            if runtime.last_bar_key == bar_key:
                return False

        compiled = self._compiled[alert.alert_id]
        if compiled.rule.timing == "bar_close" and (
            not isinstance(event, KlineEvent) or not event.confirmed
        ):
            return False
        observation = compiled.observe(
            self._context(alert, event, now), event=event, drawing=self._drawing_for,
            levels=levels, state=runtime.rule_state, consume_retests=False,
        )
        if observation is None:
            return False
        if isinstance(event, KlineEvent):
            runtime.last_bar_key = (event.bar.timestamp, event.revision)

        previous = runtime.condition_true if not runtime.baseline_pending else None
        runtime.baseline_pending = False
        runtime.condition_true = observation.matched
        if not observation.matched:
            return False
        if observation.crossed_at is not None:
            if runtime.last_cross_ts == observation.crossed_at:
                return False
            runtime.last_cross_ts = observation.crossed_at
        elif previous is not False:
            return False

        self._trigger(alert, observation, now)
        runtime.rule_state.consume_matches()
        return True

    def _context(self, alert: AlertRecord, event: FeedEvent, now: int) -> Ctx:
        key = (alert.instrument_id, alert.timeframe)
        bars = list(self._windows[key])
        cutoff = now
        bar_ts = None
        if isinstance(event, KlineEvent):
            if event.confirmed:
                bar_ts = event.bar.timestamp
                cutoff = max(event.bar.ts_available, bar_ts + TIMEFRAME_MS[alert.timeframe])
                if event.revision > 0 and bars and bars[-1].timestamp > bar_ts:
                    # A received revision to an earlier slot can update the latest indicator.
                    cutoff = max(cutoff, getattr(event, "ts_ingested", now))
                    bar_ts = None
            else:
                cutoff = max(event.bar.timestamp, getattr(event, "ts_ingested", now))
                # Even a delayed open update cannot expose the bar it belongs to.
                bar_ts = event.bar.timestamp - TIMEFRAME_MS[alert.timeframe]
        elif not isinstance(event, FeedHealthEvent):
            cutoff = event.ts_ingested

        prefix = self._session_history.get(key, ())
        if bars and prefix:
            day_ms = TIMEFRAME_MS["1d"]
            session_start = bars[-1].timestamp // day_ms * day_ms
            first_ts = bars[0].timestamp
            if first_ts < session_start:
                del self._session_history[key]
                prefix = ()
            else:
                earlier = tuple(bar for bar in prefix if bar.timestamp < first_ts)
                prefix = (
                    earlier if earlier and earlier[-1].timestamp + TIMEFRAME_MS[alert.timeframe] == first_ts
                    else ()
                )

        def read_derivatives(instrument, kind, *, start, end, as_of):
            timeframe = {
                DerivativeKind.FUNDING_RATE: None,
                DerivativeKind.MARK_PRICE: alert.timeframe,
                DerivativeKind.OPEN_INTEREST: OI_TIMEFRAME_BY_TIMEFRAME[alert.timeframe],
            }[kind]
            return [
                DerivativePoint(
                    instrument_id=instrument, kind=kind, timeframe=timeframe, value=value,
                    ts_event=tick.ts_event, ts_available=max(tick.ts_event, tick.ts_ingested),
                    ts_ingested=tick.ts_ingested, ts_committed=tick.ts_ingested,
                )
                for tick in self._derivatives.get(instrument, ())
                if start <= tick.ts_event <= end and max(tick.ts_event, tick.ts_ingested) <= as_of
                and (value := getattr(tick, kind.value)) is not None
            ]

        return Ctx(
            *key, 0, cutoff, _bar_ts=bar_ts, _indicator_defaults={}, _confirmed_compact=bars,
            _indicator_history_limit=self._bar_limit, _read_derivatives=read_derivatives, _session_prefix=prefix,
        )

    def _levels_for_event(self, event: KlineEvent, now: int) -> Sequence[KeyLevel]:
        instrument_id, timeframe = event.instrument_id, event.timeframe
        if event.confirmed:
            cutoff_ts = event.bar.timestamp + TIMEFRAME_MS[timeframe]
        else:
            cutoff_ts = event.ts_ingested if isinstance(event, ReceivedKlineEvent) else now
        provider = self._key_level_provider
        if provider is not None:
            return provider(instrument_id, timeframe, cutoff_ts)
        window = self._windows.get((instrument_id, timeframe))
        if not window:
            return []
        if not event.confirmed:
            cutoff_ts = min(cutoff_ts, event.bar.timestamp)
        try:
            return compute_key_levels(
                instrument_id=instrument_id,
                timeframe=timeframe,
                bars=list(window)[-self._bar_limit:],
                cutoff_ts=cutoff_ts,
            )
        except Exception:
            logger.exception("重算关键位失败：%s %s", instrument_id, timeframe)
            return []

    def _trigger(self, alert: AlertRecord, observation: RuleObservation, now: int) -> None:
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
            condition_kind=_trigger_kind(alert),
            triggered_ts=now,
            snapshot_id=snapshot_id,
            observed=observation.observed,
            threshold=(
                None if isinstance(alert.condition.expression, RuleComparison)
                and isinstance(alert.condition.expression.left, RuleIndicator)
                and isinstance(alert.condition.expression.right, RuleIndicator)
                and alert.condition.expression.test in {"gte", "lte"}
                else observation.threshold
            ),
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
    def _message(alert: AlertRecord, observation: RuleObservation) -> str:
        node = alert.condition.expression
        if isinstance(node, RuleLevelEvent):
            return _level_message(observation)
        if isinstance(node, RuleComparison) and node.test in {"gte", "lte"}:
            direction = "上穿" if node.test == "gte" else "下穿"
            if isinstance(node.left, RuleEvent) and isinstance(node.right, RuleConstant):
                label = _METRIC_LABELS[node.left.metric]
                return f"{label}{direction} {_decimal_text(observation.threshold)}"
            if isinstance(node.left, RuleIndicator):
                if isinstance(node.right, RuleIndicator):
                    return f"{_indicator_label(node.left)} {direction} {_indicator_label(node.right)}"
                if isinstance(node.right, RuleConstant):
                    return f"{_indicator_label(node.left)} {direction} {_decimal_text(observation.threshold)}"
        if not isinstance(node, RuleOverlay):
            return "规则触发"
        direction = "上穿" if node.direction is AlertDirection.ABOVE else "下穿"
        drawing_kind = observation.drawing_kind
        label = _OVERLAY_LABELS.get(drawing_kind, "画线") if drawing_kind else "画线"
        edge = ""
        if drawing_kind in _THICK_OVERLAYS:
            edge = "上沿" if node.direction is AlertDirection.ABOVE else "下沿"
        action = (
            "收盘突破"
            if node.mode is AlertOverlayMode.CLOSE_BREAK
            else "触碰"
        )
        return f"价格{action}{label}{edge} {_decimal_text(observation.threshold)}"


def _trigger_kind(alert: AlertRecord) -> AlertTriggerKind:
    node = alert.condition.expression
    if isinstance(node, RuleOverlay):
        return AlertTriggerKind.OVERLAY_CROSS
    if isinstance(node, RuleLevelEvent):
        return AlertTriggerKind.LEVEL_EVENT
    if isinstance(node, RuleComparison) and node.test in {"gte", "lte"}:
        if isinstance(node.left, RuleEvent) and isinstance(node.right, RuleConstant):
            return AlertTriggerKind.METRIC_THRESHOLD
        if isinstance(node.left, RuleIndicator) and isinstance(node.right, (RuleConstant, RuleIndicator)):
            return AlertTriggerKind.INDICATOR_SIGNAL
    return AlertTriggerKind.RULE


def _indicator_label(node: RuleIndicator) -> str:
    name = node.indicator_name
    period = spec_from_params(name, node.params.model_dump(exclude_none=True)).period
    return f"{name.value}({period})"


def _level_message(observation: RuleObservation) -> str:
    level = observation.level
    bounds = level_bounds(level) if level is not None else None
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
