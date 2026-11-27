"""确定性的纯数值扫描引擎。

设计约束（任务单 15 / ADR-0004）：

- 只消费 ``MarketEventFeed`` 语义的事件（K 线 + 健康 + 衍生品读数）与 07 的
  资金费率 / 持仓量序列，不导入任何模型客户端，也不发起网络请求；
- ``STALE`` 或最近一根为 ``FEED_GAP`` 的标的整轮跳过，避免断流后的假异动；
- 命中后先调用冻结回调拿到 ``snapshot_id``，再生成 Trigger；
- 同一 ``(instrument, kind)`` 冷却期内、或同一根确认柱上只生成一次；
- 扫描顺序、阈值和窗口全部显式配置，输入相同则输出确定（ID 为内容哈希）。
"""

from __future__ import annotations

import hashlib
import logging
from bisect import bisect_right
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from statistics import fmean, median, pstdev

from talaria.components import CompiledRule, Ctx, RuleState, compile_rule
from talaria.components.rules import event_visible_at
from talaria.foundation.drawings import DrawingRecord
from talaria.foundation.operators import MAX_PERIOD
from talaria.foundation.rules import (
    AlertLevelEvent, Rule, RuleLevelEvent, RuleLevelPrice, RuleStructureEvent, RuleStructurePrice,
)
from talaria.foundation.bars import BarStatus, CompactBar
from talaria.foundation.derivatives import (
    OI_TIMEFRAME_BY_TIMEFRAME,
    DerivativeKind,
    DerivativePoint,
    latest_visible_derivatives,
)
from talaria.foundation.feed import (
    DerivativeTickEvent,
    FeedHealthEvent,
    FeedHealthStatus,
    KlineEvent,
)
from talaria.foundation.key_levels import KeyLevel, KeyLevelKind, compute_key_levels
from talaria.foundation.scanner.contracts import (
    ScannerConditionKind,
    ScannerEvidence,
    ScannerSeverity,
    ScannerTrigger,
    ScannerTriggerRoute,
)
from talaria.foundation.timeframes import SUPPORTED_TIMEFRAMES, timeframe_ms
from talaria.perception.confirmed_history import ConfirmedHistory
from talaria.perception.rule_derivatives import ArchiveReader, RevisionSeries, RuleDerivatives

logger = logging.getLogger(__name__)

FreezeSnapshot = Callable[[str, str, int], str]
KeyLevelsProvider = Callable[..., Sequence[KeyLevel]]
RuleLevelsAt = Callable[[str, str, int], Sequence[KeyLevel]]

KIND_ORDER: tuple[ScannerConditionKind, ...] = (
    ScannerConditionKind.LIQUIDITY_ANOMALY,
    ScannerConditionKind.VOLATILITY_EXPANSION,
    ScannerConditionKind.FUNDING_EXTREME,
    ScannerConditionKind.OI_DIVERGENCE,
    ScannerConditionKind.KEY_LEVEL_EVENT,
)

KIND_LABELS: dict[ScannerConditionKind, str] = {
    ScannerConditionKind.LIQUIDITY_ANOMALY: "流动性异常",
    ScannerConditionKind.VOLATILITY_EXPANSION: "波动率扩张",
    ScannerConditionKind.FUNDING_EXTREME: "资金费率极值",
    ScannerConditionKind.OI_DIVERGENCE: "持仓量背离",
    ScannerConditionKind.KEY_LEVEL_EVENT: "关键位异动",
    ScannerConditionKind.RULE: "规则命中",
}

KEY_LEVEL_LABELS: dict[KeyLevelKind, str] = {
    KeyLevelKind.SWING_HIGH: "摆动高点",
    KeyLevelKind.SWING_LOW: "摆动低点",
    KeyLevelKind.PREV_DAY_HIGH: "前日高点",
    KeyLevelKind.PREV_DAY_LOW: "前日低点",
    KeyLevelKind.PREV_DAY_CLOSE: "前日收盘",
    KeyLevelKind.WEEK_OPEN: "周开盘",
    KeyLevelKind.MONTH_OPEN: "月开盘",
    KeyLevelKind.OPENING_RANGE: "开盘区间",
    KeyLevelKind.SESSION_HIGH: "时段高点",
    KeyLevelKind.SESSION_LOW: "时段低点",
    KeyLevelKind.GAP: "跳空缺口",
    KeyLevelKind.ROUND_NUMBER: "整数关口",
    KeyLevelKind.POC: "成交密集区",
    KeyLevelKind.VAH: "价值区上沿",
    KeyLevelKind.VAL: "价值区下沿",
    KeyLevelKind.ANCHORED_VWAP: "锚定 VWAP",
    KeyLevelKind.LIQUIDATION_ZONE: "清算密集区",
}


@dataclass(frozen=True, slots=True)
class ScannerConfig:
    """扫描窗口与阈值；所有阈值都可在装配处覆盖。"""

    timeframe: str = "1m"
    max_bars: int = 300

    liquidity_window: int = 60
    liquidity_min_samples: int = 20
    liquidity_zscore: float = 2.5
    liquidity_quantile: float = 0.98

    volatility_window: int = 60
    volatility_min_samples: int = 20
    volatility_quantile: float = 0.95
    volatility_ratio: float = 1.5
    atr_period: int = 14

    funding_lookback: int = 90
    funding_min_samples: int = 24
    funding_high_quantile: float = 0.95
    funding_low_quantile: float = 0.05
    funding_max_age_ms: int = 36 * 60 * 60 * 1000

    oi_lookback: int = 6
    oi_min_samples: int = 3
    oi_price_move_min: float = 0.005
    oi_change_min: float = 0.01

    key_level_min_strength: float = 0.7
    key_level_touch_tolerance: float = 0.001

    cooldown_ms: int = 5 * 60 * 1000
    focus_ttl_ms: int = 30 * 60 * 1000

    def __post_init__(self) -> None:
        if self.timeframe not in SUPPORTED_TIMEFRAMES:
            raise ValueError(f"不支持的扫描周期：{self.timeframe}")
        for name in (
            "max_bars",
            "liquidity_window",
            "liquidity_min_samples",
            "volatility_window",
            "volatility_min_samples",
            "atr_period",
            "funding_lookback",
            "funding_min_samples",
            "oi_lookback",
            "oi_min_samples",
            "cooldown_ms",
            "focus_ttl_ms",
        ):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} 必须为正数")
        if not 0 < self.liquidity_quantile < 1:
            raise ValueError("liquidity_quantile 必须在 (0, 1) 内")
        if not 0 < self.volatility_quantile < 1:
            raise ValueError("volatility_quantile 必须在 (0, 1) 内")
        if not 0 < self.funding_high_quantile < 1:
            raise ValueError("funding_high_quantile 必须在 (0, 1) 内")
        if not 0 < self.funding_low_quantile < self.funding_high_quantile:
            raise ValueError("funding_low_quantile 必须小于高位阈值")
        if self.liquidity_zscore <= 0 or self.volatility_ratio <= 0:
            raise ValueError("z-score / ATR 倍数阈值必须为正数")
        if self.oi_price_move_min <= 0 or self.oi_change_min <= 0:
            raise ValueError("OI 背离阈值必须为正数")
        if not 0 <= self.key_level_min_strength <= 1:
            raise ValueError("key_level_min_strength 必须在 [0, 1] 内")
        if self.key_level_touch_tolerance <= 0:
            raise ValueError("key_level_touch_tolerance 必须为正数")


@dataclass(frozen=True, slots=True)
class _Hit:
    kind: ScannerConditionKind
    severity: ScannerSeverity
    title: str
    summary: str
    evidence: list[ScannerEvidence]
    tags: list[str]


@dataclass(frozen=True, slots=True)
class _KeyLevelCandidate:
    level: KeyLevel
    breakout: bool
    distance: float


def _percentile_rank(values: Sequence[float], point: float) -> float:
    """弱分位：小于记 0，等于记半，避免常量序列被误判为极端。"""

    if not values:
        return 0.0
    less = sum(1 for value in values if value < point)
    equal = sum(1 for value in values if value == point)
    return (less + 0.5 * equal) / len(values)


def _decimal(value: float | Decimal, places: Decimal = Decimal("0.000001")) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(round(value, 6))).quantize(places)


def _mean(values: Sequence[float]) -> float:
    return fmean(values) if values else 0.0


def _true_ranges(bars: Sequence[CompactBar]) -> list[float]:
    ranges: list[float] = []
    for previous, current in zip(bars, bars[1:]):
        high = float(current.high or 0)
        low = float(current.low or 0)
        previous_close = float(previous.close or 0)
        ranges.append(
            max(high - low, abs(high - previous_close), abs(low - previous_close))
        )
    return ranges


class ScannerEngine:
    """维护行情缓冲区并按五类条件做批量扫描。"""

    def __init__(
        self,
        *,
        now_ms: Callable[[], int],
        config: ScannerConfig | None = None,
        freeze_snapshot: FreezeSnapshot,
        key_levels_provider: KeyLevelsProvider | None = None,
        rule_levels_at: RuleLevelsAt | None = None,
        read_archived_derivatives: ArchiveReader | None = None,
    ) -> None:
        self._now_ms = now_ms
        self._config = config or ScannerConfig()
        self._freeze_snapshot = freeze_snapshot
        self._rule_levels_at = rule_levels_at
        self._key_levels_provider = key_levels_provider or compute_key_levels

        self._bars: dict[tuple[str, str], ConfirmedHistory] = {}
        self._stale: set[str] = set()
        self._derivatives = RuleDerivatives(self._config.max_bars * 4, read_archived_derivatives)

        self._rule_hits: dict[tuple[str, str], tuple[int, int]] = {}
        self._last_trigger_at: dict[tuple[str, ScannerConditionKind], int] = {}
        self._last_trigger_bar: dict[tuple[str, ScannerConditionKind], int] = {}
        self._focus: dict[str, int] = {}
        self._snapshot_cache: dict[tuple[str, str, int], str] = {}
        self._level_cache: dict[tuple[str, str, int], tuple[KeyLevel, ...]] = {}
        self._universe: set[str] = set()

    # ------------------------------------------------------------------ 配置

    @property
    def config(self) -> ScannerConfig:
        return self._config

    @property
    def timeframe(self) -> str:
        return self._config.timeframe

    # ------------------------------------------------------------------ 事件

    def handle_event(self, event: object) -> None:
        """同步的 FeedListener 入口；只处理扫描所需的事件类型。"""

        if isinstance(event, KlineEvent):
            self._ingest_bar(event)
        elif isinstance(event, FeedHealthEvent):
            self._ingest_health(event)
        elif isinstance(event, DerivativeTickEvent):
            self._derivatives.handle_tick(event)

    def _ingest_bar(self, event: KlineEvent) -> None:
        if event.timeframe != self._config.timeframe or not event.confirmed:
            return
        self._history(event.instrument_id, event.timeframe).put(event.bar, event.revision, visible_at=event_visible_at(event))
        self._snapshot_cache.clear()
        self._level_cache.clear()

    def _ingest_health(self, event: FeedHealthEvent) -> None:
        if event.status is FeedHealthStatus.STALE:
            self._stale.add(event.instrument_id)
        elif event.status is FeedHealthStatus.LIVE:
            self._stale.discard(event.instrument_id)

    def seed_bars(
        self,
        instrument_id: str,
        timeframe: str,
        bars: Iterable[CompactBar],
    ) -> None:
        """从冷启动历史（如 07/连接器回填）灌入确认柱。"""

        history = self._history(instrument_id, timeframe)
        for bar in bars:
            history.put(bar)
        self._snapshot_cache.clear()
        self._level_cache.clear()

    def seed_derivatives(
        self,
        instrument_id: str,
        points: Iterable[DerivativePoint],
    ) -> None:
        """灌入 07 的资金费率 / 持仓量历史。"""

        self._derivatives.seed(point for point in points if point.instrument_id == instrument_id)

    def _history(self, instrument: str, timeframe: str) -> ConfirmedHistory:
        key = (instrument, timeframe)
        if key not in self._bars:
            self._bars[key] = ConfirmedHistory(timeframe_ms(timeframe), max(self._config.max_bars, MAX_PERIOD + 2))
        return self._bars[key]

    # ------------------------------------------------------------------ 扫描

    def scan(
        self,
        instrument_ids: Iterable[str] | None = None,
        *,
        now: int | None = None,
        rule: Rule | None = None,
        drawing: Callable[[str], DrawingRecord | None] | None = None,
        include_gate_hits: bool = False,
    ) -> list[ScannerTrigger]:
        """扫描给定标的；``instrument_ids`` 省略时沿用上一次的范围。"""

        compiled = None
        fingerprint = ""
        if rule is not None:
            if rule.timing != "bar_close":
                raise ValueError("手动扫描只支持收盘求值规则")
            compiled = compile_rule(rule)
            fingerprint = hashlib.sha256(rule.model_dump_json().encode()).hexdigest()

        timestamp = self._now_ms() if now is None else now
        self._expire_focus(timestamp)
        if instrument_ids is not None:
            self._universe = set(instrument_ids)
        # 保留窗口内同一柱的去重记录，即使焦点或冷却已经过期。
        self._rule_hits = {
            key: hit for key, hit in self._rule_hits.items()
            if timestamp - hit[1] < self._config.cooldown_ms
            or hit[0] in self._bars.get((key[0], self.timeframe), ())
        }

        triggers: list[ScannerTrigger] = []
        for instrument_id in sorted(self._universe):
            if instrument_id in self._stale:
                continue
            history = self._history(instrument_id, self.timeframe)
            visible = history.visible(timestamp)
            if not visible or visible[-1].status is BarStatus.FEED_GAP:
                continue
            bars = [bar for bar in visible if bar.status is not BarStatus.FEED_GAP][-self._config.max_bars:]
            revisions = self._derivatives.observation(instrument_id, timestamp)
            if compiled is not None:
                match = self._evaluate(instrument_id, ScannerConditionKind.RULE, timestamp, bars, revisions,
                                       rule=compiled, fingerprint=fingerprint, drawing=drawing)
                if match is None:
                    continue
                triggers.append(match)
                if not include_gate_hits:
                    continue
            for kind in KIND_ORDER:
                trigger = self._evaluate(instrument_id, kind, timestamp, bars, revisions)
                if trigger is not None:
                    triggers.append(trigger)
        return triggers

    def active_focus(self, now: int | None = None) -> list[str]:
        """返回仍在焦点 TTL 内的标的，按最近命中时间倒序。"""

        timestamp = self._now_ms() if now is None else now
        self._expire_focus(timestamp)
        return [
            instrument_id
            for instrument_id, last_ts in sorted(
                self._focus.items(), key=lambda item: (-item[1], item[0])
            )
            if timestamp - last_ts < self._config.focus_ttl_ms
        ]

    def focus_last_trigger_ts(self, instrument_id: str) -> int | None:
        return self._focus.get(instrument_id)

    def last_trigger_at(
        self, instrument_id: str, kind: ScannerConditionKind
    ) -> int | None:
        return self._last_trigger_at.get((instrument_id, kind))

    def _expire_focus(self, now: int) -> None:
        expired = [
            instrument_id
            for instrument_id, last_ts in self._focus.items()
            if now - last_ts >= self._config.focus_ttl_ms
        ]
        for instrument_id in expired:
            self._focus.pop(instrument_id, None)

    def _evaluate(
        self,
        instrument_id: str,
        kind: ScannerConditionKind,
        now: int,
        bars: Sequence[CompactBar],
        revisions: RevisionSeries,
        *,
        rule: CompiledRule | None = None,
        fingerprint: str = "",
        drawing: Callable[[str], DrawingRecord | None] | None = None,
    ) -> ScannerTrigger | None:
        bar_ts = bars[-1].timestamp
        if rule is not None:
            # Repeated manual requests for different rules have independent cooldowns.
            rule_key = (instrument_id, fingerprint)
            last_ts, last_at = self._rule_hits.get(rule_key, (None, None))
        else:
            last_ts = self._last_trigger_bar.get((instrument_id, kind))
            last_at = self._last_trigger_at.get((instrument_id, kind))
        if last_ts is not None and bars[-1].timestamp <= last_ts:
            return None
        if last_at is not None and now - last_at < self._config.cooldown_ms:
            return None

        if rule is not None:
            hit = self._rule_hit(instrument_id, bars[-1], rule, drawing, revisions)
        elif kind is ScannerConditionKind.LIQUIDITY_ANOMALY:
            hit = self._liquidity_hit(instrument_id, bars)
        elif kind is ScannerConditionKind.VOLATILITY_EXPANSION:
            hit = self._volatility_hit(instrument_id, bars)
        elif kind is ScannerConditionKind.FUNDING_EXTREME:
            hit = self._funding_hit(revisions, now)
        elif kind is ScannerConditionKind.OI_DIVERGENCE:
            hit = self._oi_hit(revisions, bars, now)
        else:
            hit = self._key_level_hit(instrument_id, bars, now, revisions)
        if hit is None:
            return None

        as_of = max(bar_ts + timeframe_ms(self.timeframe), bars[-1].ts_available) if rule is not None else now
        snapshot_id = self._snapshot(instrument_id, as_of)
        if not snapshot_id:
            # 没有冻结证据的异动不允许进入工作台，避免无法回看的卡片。
            return None

        trigger = ScannerTrigger(
            trigger_id=self._trigger_id(instrument_id, kind, bar_ts, fingerprint),
            instrument_id=instrument_id,
            timeframe=self._config.timeframe,
            kind=kind,
            severity=hit.severity,
            title=hit.title,
            summary=hit.summary,
            evidence=hit.evidence,
            tags=hit.tags,
            created_ts=now,
            snapshot_id=snapshot_id,
            route=ScannerTriggerRoute.LOCAL,
        )
        if rule is not None:
            self._rule_hits[(instrument_id, fingerprint)] = (bar_ts, now)
        else:
            self._last_trigger_at[(instrument_id, kind)] = now
            self._last_trigger_bar[(instrument_id, kind)] = bar_ts
        self._focus[instrument_id] = now
        return trigger

    def _trigger_id(
        self, instrument_id: str, kind: ScannerConditionKind, bar_ts: int, fingerprint: str = "",
    ) -> str:
        payload = f"{instrument_id}|{self._config.timeframe}|{kind.value}|{bar_ts}"
        if fingerprint:
            payload += f"|{fingerprint}"
        digest = hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]
        return f"scn_{digest}"

    def _rule_hit(
        self, instrument_id: str, current: CompactBar,
        rule: CompiledRule, drawing: Callable[[str], DrawingRecord | None] | None,
        revisions: RevisionSeries,
    ) -> _Hit | None:
        timeframe = self.timeframe
        interval = timeframe_ms(timeframe)
        history = self._history(instrument_id, timeframe)

        level_nodes = [node for node in rule.rule.references() if isinstance(node, (RuleLevelEvent, RuleLevelPrice))]
        retest = any(isinstance(node, (RuleLevelEvent, RuleStructureEvent)) and node.event is AlertLevelEvent.RETEST for node in rule.rule.references())
        state = RuleState()

        def evaluate_at(slot: int, cutoff: int):
            # A manual scan examines data known at the latest bar's confirmation.
            # Historical levels are also read at each bar's own cutoff.
            closed = history.visible(cutoff, through=slot)
            levels = {}
            if level_nodes:
                close_ts = slot + interval
                if self._rule_levels_at is not None:
                    available = self._rule_levels_at(instrument_id, timeframe, close_ts)
                else:
                    available = self._key_levels_provider(
                        instrument_id=instrument_id, timeframe=timeframe, bars=history.visible(close_ts, through=slot),
                        cutoff_ts=close_ts, derivative_points=self._key_level_derivatives(revisions, close_ts),
                    )
                levels = {level.level_id: level for level in available}
            ctx = Ctx(
                instrument_id, timeframe, 0, cutoff, drawing=drawing, levels=levels, rule_state=state,
                _bar_ts=slot, _indicator_defaults={}, _confirmed_compact=closed,
                _first_confirmed_at=history.first_confirmed_at, _read_derivative_revisions=revisions,
            )
            return rule.observe(ctx)

        cutoff = max(current.timestamp + interval, current.ts_available)
        if retest:
            for previous_cutoff, slot in history.observations_before(current.timestamp, cutoff):
                evaluate_at(slot, previous_cutoff)
        observation = evaluate_at(current.timestamp, cutoff)
        if observation is None or not observation.matched:
            return None
        evidence = []
        if observation.observed is not None:
            evidence.append(ScannerEvidence(metric="rule", value=observation.observed,
                                            threshold=observation.threshold))
        return _Hit(ScannerConditionKind.RULE, ScannerSeverity.INFO, "规则命中",
                    "收盘规则成立", evidence, [])

    def _snapshot(self, instrument_id: str, now: int) -> str:
        key = (instrument_id, self._config.timeframe, now)
        cached = self._snapshot_cache.get(key)
        if cached is not None:
            return cached
        try:
            snapshot_id = self._freeze_snapshot(
                instrument_id, self._config.timeframe, now
            )
        except Exception:
            logger.exception("冻结扫描证据快照失败：%s", instrument_id)
            snapshot_id = ""
        if len(self._snapshot_cache) > 64:
            self._snapshot_cache.clear()
        self._snapshot_cache[key] = snapshot_id
        return snapshot_id

    # -------------------------------------------------------------- 五类条件

    def _liquidity_hit(
        self, instrument_id: str, bars: Sequence[CompactBar]
    ) -> _Hit | None:
        config = self._config
        window = list(bars[-config.liquidity_window :])
        if len(window) < config.liquidity_min_samples + 1:
            return None
        baseline, current = window[:-1], window[-1]
        use_turnover = any(bar.turnover for bar in baseline)
        metric_name = "成交额" if use_turnover else "成交量"

        def metric(bar: CompactBar) -> float:
            value = bar.turnover if use_turnover else bar.volume
            return float(value or 0)

        values = [metric(bar) for bar in baseline]
        current_value = metric(current)
        if current_value <= 0 or len(values) < config.liquidity_min_samples:
            return None
        mean = _mean(values)
        deviation = pstdev(values)
        zscore = (current_value - mean) / deviation if deviation > 0 else 0.0
        quantile = _percentile_rank(values, current_value)
        median_value = median(values)
        ratio = current_value / median_value if median_value > 0 else 0.0
        if current_value <= mean:
            return None
        if zscore < config.liquidity_zscore and quantile < config.liquidity_quantile:
            return None
        severity = (
            ScannerSeverity.CRITICAL
            if zscore >= 4.0 or quantile >= 0.995
            else ScannerSeverity.WARNING
        )
        summary = (
            f"{metric_name}较近 {len(values)} 根均值放大 "
            f"{zscore:.1f}σ（分位 {quantile:.0%}，倍数 {ratio:.2f}×）"
        )
        return _Hit(
            kind=ScannerConditionKind.LIQUIDITY_ANOMALY,
            severity=severity,
            title="流动性异常",
            summary=summary,
            evidence=[
                ScannerEvidence(metric="zscore", value=_decimal(zscore)),
                ScannerEvidence(metric="quantile", value=_decimal(quantile)),
                ScannerEvidence(metric="ratio", value=_decimal(ratio)),
                ScannerEvidence(
                    metric="value", value=_decimal(current_value)
                ),
            ],
            tags=["流动性", self._config.timeframe, f"z={zscore:.1f}"],
        )

    def _volatility_hit(
        self, instrument_id: str, bars: Sequence[CompactBar]
    ) -> _Hit | None:
        config = self._config
        window = list(bars[-(config.volatility_window + config.atr_period) :])
        ranges = _true_ranges(window)
        if len(ranges) < config.volatility_min_samples + 1:
            return None
        current_tr = ranges[-1]
        baseline = ranges[-(config.volatility_window + 1) : -1]
        if len(baseline) < config.volatility_min_samples:
            return None
        atr = _mean(baseline[-config.atr_period :])
        quantile = _percentile_rank(baseline, current_tr)
        ratio = current_tr / atr if atr > 0 else 0.0
        if quantile < config.volatility_quantile or ratio < config.volatility_ratio:
            return None
        severity = (
            ScannerSeverity.CRITICAL
            if ratio >= 2.5 or quantile >= 0.99
            else ScannerSeverity.WARNING
        )
        return _Hit(
            kind=ScannerConditionKind.VOLATILITY_EXPANSION,
            severity=severity,
            title="波动率扩张",
            summary=(
                f"真实波幅 {current_tr:.2f} 为近 {config.atr_period} 根 ATR 的 "
                f"{ratio:.2f} 倍（分位 {quantile:.0%}）"
            ),
            evidence=[
                ScannerEvidence(metric="true_range", value=_decimal(current_tr)),
                ScannerEvidence(metric="atr", value=_decimal(atr)),
                ScannerEvidence(metric="ratio", value=_decimal(ratio)),
                ScannerEvidence(metric="quantile", value=_decimal(quantile)),
            ],
            tags=["波动率", self._config.timeframe, f"×{ratio:.1f}"],
        )

    def _funding_hit(self, revisions: RevisionSeries, now: int) -> _Hit | None:
        config = self._config
        points = [(p.ts_event, p.value) for p in latest_visible_derivatives(
            (p for p in revisions(DerivativeKind.FUNDING_RATE) if p.is_funding_settlement), as_of=now,
        )]
        if len(points) < config.funding_min_samples + 1:
            return None
        latest_ts, latest = points[-1]
        if now - latest_ts > config.funding_max_age_ms:
            return None
        history = [float(value) for _, value in points[-(config.funding_lookback + 1) : -1]]
        if len(history) < config.funding_min_samples:
            return None
        value = float(latest)
        quantile = _percentile_rank(history, value)
        high = quantile >= config.funding_high_quantile
        low = quantile <= config.funding_low_quantile
        if not high and not low:
            return None
        severity = (
            ScannerSeverity.CRITICAL
            if quantile >= 0.99 or quantile <= 0.01
            else ScannerSeverity.WARNING
        )
        side = "高位" if high else "低位"
        return _Hit(
            kind=ScannerConditionKind.FUNDING_EXTREME,
            severity=severity,
            title="资金费率极值",
            summary=(
                f"资金费率 {value * 100:+.4f}% 处于近 {len(history)} 次结算的 "
                f"{quantile:.0%} 分位（{side}极值）"
            ),
            evidence=[
                ScannerEvidence(metric="funding_rate", value=latest),
                ScannerEvidence(metric="quantile", value=_decimal(quantile)),
                ScannerEvidence(
                    metric="settlements", value=Decimal(len(history))
                ),
            ],
            tags=["资金费率", f"{side}极值", self._config.timeframe],
        )

    def _visible_oi(self, revisions: RevisionSeries, now: int) -> list[DerivativePoint]:
        timeframe = OI_TIMEFRAME_BY_TIMEFRAME[self.timeframe]
        return latest_visible_derivatives(
            (p for p in revisions(DerivativeKind.OPEN_INTEREST) if p.timeframe in (None, timeframe)), as_of=now,
        )

    def _key_level_derivatives(self, revisions: RevisionSeries, cutoff: int) -> list[DerivativePoint]:
        # 内置关键位只用持仓量，自定义提供方仍可消费其余可见的衍生品序列。
        points = self._visible_oi(revisions, cutoff)
        if self._key_levels_provider is not compute_key_levels:
            points.extend(latest_visible_derivatives(revisions(DerivativeKind.MARK_PRICE), as_of=cutoff))
            points.extend(latest_visible_derivatives(
                (p for p in revisions(DerivativeKind.FUNDING_RATE) if p.is_funding_settlement), as_of=cutoff,
            ))
        return points

    def _oi_hit(
        self,
        revisions: RevisionSeries,
        bars: Sequence[CompactBar],
        now: int,
    ) -> _Hit | None:
        config = self._config
        oi_timeframe = OI_TIMEFRAME_BY_TIMEFRAME.get(self._config.timeframe, "5m")
        visible = self._visible_oi(revisions, now)
        interval = timeframe_ms(oi_timeframe)
        buckets = {p.ts_event // interval * interval: p.value for p in visible if p.timeframe is None}
        buckets.update((p.ts_event, p.value) for p in visible if p.timeframe == oi_timeframe)
        points = sorted(buckets.items())
        if len(points) < config.oi_min_samples:
            return None
        window = points[-(config.oi_lookback + 1) :]
        first_ts, first_oi = window[0]
        last_ts, last_oi = window[-1]
        if first_oi == 0:
            return None
        timestamps = [bar.timestamp for bar in bars]

        def close_at(ts: int) -> float | None:
            index = bisect_right(timestamps, ts) - 1
            if index < 0:
                return None
            return float(bars[index].close or 0)

        start_price = close_at(first_ts)
        end_price = close_at(last_ts)
        if start_price is None or end_price is None or start_price == 0:
            return None
        if end_price == start_price:
            return None
        price_change = (end_price - start_price) / start_price
        oi_change = float((last_oi - first_oi) / first_oi)
        if abs(price_change) < config.oi_price_move_min:
            return None
        if abs(oi_change) < config.oi_change_min:
            return None
        if price_change * oi_change >= 0:
            return None
        severity = (
            ScannerSeverity.CRITICAL
            if abs(price_change) >= config.oi_price_move_min * 2
            and abs(oi_change) >= config.oi_change_min * 2
            else ScannerSeverity.WARNING
        )
        direction = "价跌仓增" if price_change < 0 else "价涨仓减"
        window_minutes = max(0, round((last_ts - first_ts) / 60_000))
        return _Hit(
            kind=ScannerConditionKind.OI_DIVERGENCE,
            severity=severity,
            title="持仓量背离",
            summary=(
                f"近 {window_minutes} 分钟价格 {price_change * 100:+.2f}%，"
                f"持仓量 {oi_change * 100:+.2f}%（{direction}）"
            ),
            evidence=[
                ScannerEvidence(
                    metric="price_change", value=_decimal(price_change)
                ),
                ScannerEvidence(metric="oi_change", value=_decimal(oi_change)),
                ScannerEvidence(
                    metric="oi_points",
                    value=Decimal(len(window)),
                    note=oi_timeframe,
                ),
            ],
            tags=["OI 背离", direction, oi_timeframe],
        )

    def _key_level_hit(
        self,
        instrument_id: str,
        bars: Sequence[CompactBar],
        now: int,
        revisions: RevisionSeries,
    ) -> _Hit | None:
        config = self._config
        if len(bars) < 3:
            return None
        current = bars[-1]
        previous = bars[-2]
        cache_key = (instrument_id, self._config.timeframe, current.timestamp)
        levels = self._level_cache.get(cache_key)
        if levels is None:
            try:
                levels = tuple(
                    self._key_levels_provider(
                        instrument_id=instrument_id,
                        timeframe=self._config.timeframe,
                        bars=list(bars),
                        cutoff_ts=now,
                        derivative_points=self._key_level_derivatives(revisions, now),
                    )
                )
            except Exception:
                logger.exception("关键位计算失败，跳过：%s", instrument_id)
                return None
            if len(self._level_cache) > 64:
                self._level_cache.clear()
            self._level_cache[cache_key] = levels

        candidates: list[_KeyLevelCandidate] = []
        previous_close = float(previous.close or 0)
        current_close = float(current.close or 0)
        current_high = float(current.high or 0)
        current_low = float(current.low or 0)
        for level in levels:
            if level.strength < config.key_level_min_strength:
                continue
            if level.formed_ts >= current.timestamp or level.ts_available > now:
                continue
            if level.price is not None:
                low = high = float(level.price)
            elif level.price_low is not None and level.price_high is not None:
                low = float(level.price_low)
                high = float(level.price_high)
            else:
                continue
            if low <= 0 or high <= 0:
                continue
            tolerance = high * config.key_level_touch_tolerance
            breakout = (
                previous_close < low - tolerance
                and current_close > high + tolerance
            ) or (
                previous_close > high + tolerance
                and current_close < low - tolerance
            )
            touched = current_low <= high + tolerance and current_high >= low - tolerance
            if not breakout and not touched:
                continue
            mid = (low + high) / 2
            distance = min(abs(current_close - low), abs(current_close - high))
            candidates.append(
                _KeyLevelCandidate(
                    level=level,
                    breakout=breakout,
                    distance=distance / mid if mid > 0 else 0.0,
                )
            )
        if not candidates:
            return None
        best = max(
            candidates,
            key=lambda item: (
                1 if item.breakout else 0,
                item.level.strength,
                -item.distance,
            ),
        )
        level = best.level
        label = KEY_LEVEL_LABELS.get(level.kind, level.kind.value)
        price = float(level.price if level.price is not None else level.price_low or 0)
        if best.breakout:
            direction = "向上突破" if current_close > previous_close else "向下跌破"
            title = "关键位突破"
            summary = (
                f"{direction}{label} {price:.2f}（强度 {level.strength:.2f}，"
                f"距现价 {best.distance:.2%}），确认柱收于 {current_close:.2f}；"
                f"本柱振幅 {current_high - current_low:.2f}，突破已确认。"
            )
        else:
            title = "关键位触及"
            summary = (
                f"触及{label} {price:.2f}（强度 {level.strength:.2f}，"
                f"距现价 {best.distance:.2%}），确认柱收于 {current_close:.2f}；"
                f"本柱振幅 {current_high - current_low:.2f}，尚未形成有效突破。"
            )
        return _Hit(
            kind=ScannerConditionKind.KEY_LEVEL_EVENT,
            severity=(
                ScannerSeverity.CRITICAL
                if best.breakout and level.strength >= 0.85
                else ScannerSeverity.WARNING
            ),
            title=title,
            summary=summary,
            evidence=[
                ScannerEvidence(
                    metric="level_price",
                    value=_decimal(price),
                    note=level.level_id,
                ),
                ScannerEvidence(
                    metric="strength", value=_decimal(level.strength)
                ),
                ScannerEvidence(metric="distance", value=_decimal(best.distance)),
                ScannerEvidence(
                    metric="breakout", value=Decimal(1 if best.breakout else 0)
                ),
            ],
            tags=[
                "关键位",
                label,
                "突破" if best.breakout else "触及",
                f"强度 {level.strength:.2f}",
            ],
        )
