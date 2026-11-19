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
from dataclasses import dataclass, field
from decimal import Decimal
from statistics import fmean, median, pstdev

from talaria.foundation.bars import BarStatus, CompactBar
from talaria.foundation.derivatives import (
    OI_TIMEFRAME_BY_TIMEFRAME,
    DerivativeKind,
    DerivativePoint,
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
from talaria.foundation.types import PrecisionTier

logger = logging.getLogger(__name__)

FreezeSnapshot = Callable[[str, str, int], str]
KeyLevelsProvider = Callable[..., Sequence[KeyLevel]]

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


@dataclass(slots=True)
class _DerivativeSeries:
    funding: dict[int, tuple[int, Decimal]] = field(default_factory=dict)
    oi: dict[str, dict[int, tuple[int, Decimal]]] = field(default_factory=dict)


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
    ) -> None:
        self._now_ms = now_ms
        self._config = config or ScannerConfig()
        self._freeze_snapshot = freeze_snapshot
        self._key_levels_provider = key_levels_provider or compute_key_levels

        self._bars: dict[tuple[str, str], dict[int, CompactBar]] = {}
        self._last_gap: dict[tuple[str, str], int] = {}
        self._stale: set[str] = set()
        self._derivatives: dict[str, _DerivativeSeries] = {}
        self._oi_ticks: dict[str, dict[int, Decimal]] = {}

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
            self._ingest_derivative_tick(event)

    def _ingest_bar(self, event: KlineEvent) -> None:
        if event.timeframe != self._config.timeframe or not event.confirmed:
            return
        key = (event.instrument_id, event.timeframe)
        bar = event.bar
        if bar.status is BarStatus.FEED_GAP:
            self._last_gap[key] = max(self._last_gap.get(key, 0), bar.timestamp)
            return
        bucket = self._bars.setdefault(key, {})
        bucket[bar.timestamp] = bar
        self._trim_bars(bucket)
        self._snapshot_cache.clear()
        self._level_cache.clear()

    def _ingest_health(self, event: FeedHealthEvent) -> None:
        if event.status is FeedHealthStatus.STALE:
            self._stale.add(event.instrument_id)
        elif event.status is FeedHealthStatus.LIVE:
            self._stale.discard(event.instrument_id)

    def _ingest_derivative_tick(self, event: DerivativeTickEvent) -> None:
        """OI ticker 只作为 07 序列的补充；资金费率历史必须来自结算序列。"""

        if event.open_interest is None:
            return
        oi_timeframe = OI_TIMEFRAME_BY_TIMEFRAME.get(self._config.timeframe, "5m")
        bucket_ms = timeframe_ms(oi_timeframe)
        slot = event.ts_event // bucket_ms * bucket_ms
        bucket = self._oi_ticks.setdefault(event.instrument_id, {})
        bucket[slot] = event.open_interest
        if len(bucket) > self._config.max_bars * 4:
            for stale_slot in sorted(bucket)[: len(bucket) - self._config.max_bars * 2]:
                bucket.pop(stale_slot, None)

    def seed_bars(
        self,
        instrument_id: str,
        timeframe: str,
        bars: Iterable[CompactBar],
    ) -> None:
        """从冷启动历史（如 07/连接器回填）灌入确认柱。"""

        key = (instrument_id, timeframe)
        bucket = self._bars.setdefault(key, {})
        for bar in bars:
            if bar.status is BarStatus.FEED_GAP:
                self._last_gap[key] = max(self._last_gap.get(key, 0), bar.timestamp)
                continue
            bucket[bar.timestamp] = bar
        self._trim_bars(bucket)
        self._level_cache.clear()

    def seed_derivatives(
        self,
        instrument_id: str,
        points: Iterable[DerivativePoint],
    ) -> None:
        """灌入 07 的资金费率 / 持仓量历史。"""

        series = self._derivatives.setdefault(instrument_id, _DerivativeSeries())
        for point in points:
            if point.kind is DerivativeKind.FUNDING_RATE:
                series.funding[point.ts_event] = (point.ts_available, point.value)
            elif point.kind is DerivativeKind.OPEN_INTEREST:
                timeframe = point.timeframe or OI_TIMEFRAME_BY_TIMEFRAME.get(
                    self._config.timeframe, "5m"
                )
                bucket = series.oi.setdefault(timeframe, {})
                bucket[point.ts_event] = (point.ts_available, point.value)
        for timeframe, bucket in series.oi.items():
            if len(bucket) > self._config.max_bars * 4:
                for stale_ts in sorted(bucket)[
                    : len(bucket) - self._config.max_bars * 2
                ]:
                    bucket.pop(stale_ts, None)

    def _trim_bars(self, bucket: dict[int, CompactBar]) -> None:
        limit = max(self._config.max_bars * 2, 600)
        if len(bucket) <= limit:
            return
        for stale_ts in sorted(bucket)[: len(bucket) - self._config.max_bars]:
            bucket.pop(stale_ts, None)

    # ------------------------------------------------------------------ 扫描

    def scan(
        self,
        instrument_ids: Iterable[str] | None = None,
        *,
        now: int | None = None,
    ) -> list[ScannerTrigger]:
        """扫描给定标的；``instrument_ids`` 省略时沿用上一次的范围。"""

        timestamp = self._now_ms() if now is None else now
        self._expire_focus(timestamp)
        if instrument_ids is not None:
            self._universe = set(instrument_ids)

        triggers: list[ScannerTrigger] = []
        for instrument_id in sorted(self._universe):
            if instrument_id in self._stale:
                continue
            for kind in KIND_ORDER:
                trigger = self._evaluate(instrument_id, kind, timestamp)
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
    ) -> ScannerTrigger | None:
        bars = self._normal_bars(instrument_id, now)
        if not bars:
            return None
        key = (instrument_id, self._config.timeframe)
        gap_ts = self._last_gap.get(key, -1)
        if gap_ts > bars[-1].timestamp:
            return None

        last_ts = self._last_trigger_bar.get((instrument_id, kind))
        if last_ts is not None and bars[-1].timestamp <= last_ts:
            return None
        last_at = self._last_trigger_at.get((instrument_id, kind))
        if last_at is not None and now - last_at < self._config.cooldown_ms:
            return None

        if kind is ScannerConditionKind.LIQUIDITY_ANOMALY:
            hit = self._liquidity_hit(instrument_id, bars)
        elif kind is ScannerConditionKind.VOLATILITY_EXPANSION:
            hit = self._volatility_hit(instrument_id, bars)
        elif kind is ScannerConditionKind.FUNDING_EXTREME:
            hit = self._funding_hit(instrument_id, now)
        elif kind is ScannerConditionKind.OI_DIVERGENCE:
            hit = self._oi_hit(instrument_id, bars, now)
        else:
            hit = self._key_level_hit(instrument_id, bars, now)
        if hit is None:
            return None

        snapshot_id = self._snapshot(instrument_id, now)
        if not snapshot_id:
            # 没有冻结证据的异动不允许进入工作台，避免无法回看的卡片。
            return None

        trigger = ScannerTrigger(
            trigger_id=self._trigger_id(instrument_id, kind, bars[-1].timestamp),
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
        self._last_trigger_at[(instrument_id, kind)] = now
        self._last_trigger_bar[(instrument_id, kind)] = bars[-1].timestamp
        self._focus[instrument_id] = now
        return trigger

    def _trigger_id(
        self, instrument_id: str, kind: ScannerConditionKind, bar_ts: int
    ) -> str:
        payload = f"{instrument_id}|{self._config.timeframe}|{kind.value}|{bar_ts}"
        digest = hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]
        return f"scn_{digest}"

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

    def _normal_bars(self, instrument_id: str, cutoff: int) -> list[CompactBar]:
        key = (instrument_id, self._config.timeframe)
        bucket = self._bars.get(key)
        if not bucket:
            return []
        bars = [
            bar
            for bar in sorted(bucket.values(), key=lambda item: item.timestamp)
            if bar.status is not BarStatus.FEED_GAP
            and bar.ts_available <= cutoff
            and bar.close is not None
        ]
        return bars[-self._config.max_bars :]

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

    def _funding_series(
        self, instrument_id: str, now: int
    ) -> list[tuple[int, Decimal]]:
        series = self._derivatives.get(instrument_id)
        if series is None:
            return []
        return [
            (ts_event, value)
            for ts_event, (available, value) in sorted(series.funding.items())
            if available <= now
        ]

    def _funding_hit(self, instrument_id: str, now: int) -> _Hit | None:
        config = self._config
        points = self._funding_series(instrument_id, now)
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

    def _oi_series(
        self, instrument_id: str, timeframe: str, now: int
    ) -> list[tuple[int, Decimal]]:
        series = self._derivatives.get(instrument_id)
        points: dict[int, Decimal] = {}
        if series is not None:
            for ts_event, (available, value) in series.oi.get(timeframe, {}).items():
                if available <= now:
                    points[ts_event] = value
        for slot, value in self._oi_ticks.get(instrument_id, {}).items():
            points.setdefault(slot, value)
        return sorted(points.items())

    def _oi_hit(
        self,
        instrument_id: str,
        bars: Sequence[CompactBar],
        now: int,
    ) -> _Hit | None:
        config = self._config
        oi_timeframe = OI_TIMEFRAME_BY_TIMEFRAME.get(self._config.timeframe, "5m")
        points = self._oi_series(instrument_id, oi_timeframe, now)
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
                        derivative_points=self._all_derivative_points(instrument_id),
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

    def _all_derivative_points(self, instrument_id: str) -> list[DerivativePoint]:
        """把内部序列还原成 07 契约点，供关键位引擎估算清算区。"""

        series = self._derivatives.get(instrument_id)
        if series is None:
            return []
        points: list[DerivativePoint] = []
        precision = PrecisionTier.SECOND_VERIFIED
        for ts_event, (available, value) in sorted(series.funding.items()):
            points.append(
                DerivativePoint(
                    instrument_id=instrument_id,
                    kind=DerivativeKind.FUNDING_RATE,
                    timeframe=None,
                    value=value,
                    ts_event=ts_event,
                    ts_published=ts_event,
                    ts_available=available,
                    precision=precision,
                    ts_ingested=available,
                    ts_committed=available,
                )
            )
        for timeframe, bucket in series.oi.items():
            for ts_event, (available, value) in sorted(bucket.items()):
                points.append(
                    DerivativePoint(
                        instrument_id=instrument_id,
                        kind=DerivativeKind.OPEN_INTEREST,
                        timeframe=timeframe,
                        value=value,
                        ts_event=ts_event,
                        ts_published=ts_event,
                        ts_available=available,
                        precision=precision,
                        ts_ingested=available,
                        ts_committed=available,
                    )
                )
        return points
