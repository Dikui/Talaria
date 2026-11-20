"""关键位预警（LEVEL_EVENT）：触及、突破、回踩与状态翻转。

来源：任务单 14、docs/architecture.md §3.2.3。引擎只通过注入的
``KeyLevelProvider`` 读取“当前 cutoff 已可见”的最新关键位，不自行前视。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from talaria.foundation.alerts import (
    AlertCondition,
    AlertConditionKind,
    AlertLevelEvent,
    AlertRecord,
    AlertStatus,
)
from talaria.foundation.bars import BarStatus, CompactBar
from talaria.foundation.feed import (
    FeedHealthEvent,
    FeedHealthStatus,
    KlineEvent,
)
from talaria.foundation.key_levels import (
    EvidenceClass,
    KeyLevel,
    KeyLevelCategory,
    KeyLevelKind,
    KeyLevelStatus,
)
from talaria.foundation.types import PrecisionTier
from talaria.perception.alerts import AlertEngine

pytestmark = pytest.mark.small

INSTRUMENT = "BTC-USDT-PERP"
TIMEFRAME = "1m"
BASE_TS = 1_763_596_800_000
MINUTE = 60_000
LEVEL_ID = "kl_0123456789abcdef"


class Clock:
    def __init__(self, now: int = 1_000) -> None:
        self.now = now

    def __call__(self) -> int:
        return self.now


def _bar(
    ts: int,
    close: str,
    *,
    high: str | None = None,
    low: str | None = None,
) -> CompactBar:
    return CompactBar(
        timestamp=ts,
        open=close,
        high=high or close,
        low=low or close,
        close=close,
        volume="1",
        turnover="1",
        status=BarStatus.NORMAL,
        ts_available=ts + MINUTE,
        precision=PrecisionTier.SECOND_VERIFIED,
    )


def _kline(
    ts: int,
    close: str,
    *,
    high: str | None = None,
    low: str | None = None,
    confirmed: bool = True,
    revision: int = 0,
) -> KlineEvent:
    return KlineEvent(
        instrument_id=INSTRUMENT,
        timeframe=TIMEFRAME,
        bar=_bar(ts, close, high=high, low=low),
        confirmed=confirmed,
        revision=revision,
    )


def _health(status: FeedHealthStatus, last_event_ts: int | None = None) -> FeedHealthEvent:
    return FeedHealthEvent(
        instrument_id=INSTRUMENT,
        timeframe=TIMEFRAME,
        status=status,
        ts=BASE_TS,
        last_event_ts=last_event_ts,
        stale_since=BASE_TS if status is FeedHealthStatus.STALE else None,
        reconnect_count=0,
    )


def _level(
    status: KeyLevelStatus = KeyLevelStatus.ACTIVE,
    *,
    low: str = "100",
    high: str = "100",
    kind: KeyLevelKind = KeyLevelKind.PREV_DAY_HIGH,
) -> KeyLevel:
    return KeyLevel(
        level_id=LEVEL_ID,
        instrument_id=INSTRUMENT,
        kind=kind,
        category=KeyLevelCategory.SESSION,
        evidence_class=EvidenceClass.OBSERVED,
        timeframe=TIMEFRAME,
        price_low=low,
        price_high=high,
        strength=0.8,
        status=status,
        ts_available=BASE_TS,
        formed_ts=BASE_TS,
        touch_count=0,
        volume_weight="1",
    )


class LevelProvider:
    """测试注入的关键位来源；记录 cutoff，允许测试推进状态。"""

    def __init__(self, level: KeyLevel) -> None:
        self.level = level
        self.calls: list[tuple[str, str, str, int]] = []

    def __call__(
        self, instrument_id: str, timeframe: str, level_id: str, cutoff_ts: int
    ) -> KeyLevel | None:
        self.calls.append((instrument_id, timeframe, level_id, cutoff_ts))
        if level_id != self.level.level_id:
            return None
        return self.level


def _level_alert(
    event: AlertLevelEvent = AlertLevelEvent.TOUCH,
    *,
    alert_id: str = "alr_level",
    repeat: bool = True,
    cooldown_ms: int = 60_000,
    level_kind: KeyLevelKind | None = None,
) -> AlertRecord:
    condition = AlertCondition(
        kind=AlertConditionKind.LEVEL_EVENT,
        level_id=LEVEL_ID,
        level_event=event,
        level_kind=level_kind,
    )
    return AlertRecord(
        alert_id=alert_id,
        instrument_id=INSTRUMENT,
        timeframe=TIMEFRAME,
        condition=condition,
        status=AlertStatus.ARMED,
        repeat=repeat,
        cooldown_ms=cooldown_ms,
        created_ts=0,
        updated_ts=0,
    )


def _engine(
    provider: LevelProvider,
    *,
    clock: Clock | None = None,
    frozen: str = "snap_frozen",
) -> tuple[AlertEngine, Clock, list[str]]:
    clock = clock or Clock()
    frozen_ids: list[str] = []

    def freeze(alert: AlertRecord, now: int) -> str:
        frozen_ids.append(frozen)
        return frozen

    engine = AlertEngine(
        now_ms=clock,
        key_level_provider=provider,
        freeze_snapshot=freeze,
    )
    return engine, clock, frozen_ids


def _put(engine: AlertEngine, alert: AlertRecord) -> None:
    engine.put(alert, reset=True)


def test_touch_triggers_on_rising_edge_and_freezes_snapshot() -> None:
    provider = LevelProvider(_level())
    engine, _, frozen = _engine(provider)
    alert = _level_alert(AlertLevelEvent.TOUCH)
    _put(engine, alert)

    assert engine.handle_event(_kline(BASE_TS, "105")) == []
    changed = engine.handle_event(_kline(BASE_TS + MINUTE, "100.5", high="101", low="99"))

    assert alert.trigger_count == 1
    assert alert.status is AlertStatus.COOLDOWN
    assert frozen == ["snap_frozen"]
    record = engine.get(alert.alert_id)
    assert record is not None
    assert record.last_triggered_ts == 1_000


def test_touch_repeat_after_cooldown_needs_new_edge() -> None:
    provider = LevelProvider(_level())
    engine, clock, _ = _engine(provider)
    alert = _level_alert(AlertLevelEvent.TOUCH, cooldown_ms=1_000)
    _put(engine, alert)

    engine.handle_event(_kline(BASE_TS, "105"))
    engine.handle_event(_kline(BASE_TS + MINUTE, "100.5", high="101", low="99"))
    assert alert.trigger_count == 1

    # 冷却中：即使继续触及也不触发。
    engine.handle_event(_kline(BASE_TS + 2 * MINUTE, "100.4", high="101", low="99"))
    assert alert.trigger_count == 1

    clock.now += 1_000
    # 冷却结束后的第一根柱只重建基线，不立即触发。
    engine.handle_event(_kline(BASE_TS + 3 * MINUTE, "105"))
    assert alert.trigger_count == 1
    engine.handle_event(_kline(BASE_TS + 4 * MINUTE, "100.5", high="101", low="99"))
    assert alert.trigger_count == 2


def test_break_requires_confirmed_close_and_non_active_status() -> None:
    provider = LevelProvider(_level())
    engine, _, _ = _engine(provider)
    alert = _level_alert(AlertLevelEvent.BREAK)
    _put(engine, alert)

    engine.handle_event(_kline(BASE_TS, "100"))
    # 未确认柱即使收在关键位上方也不判定突破。
    engine.handle_event(_kline(BASE_TS + MINUTE, "101", high="101", low="100", confirmed=False))
    assert alert.trigger_count == 0

    provider.level.status = KeyLevelStatus.BROKEN
    engine.handle_event(_kline(BASE_TS + 2 * MINUTE, "101", high="101", low="100"))
    assert alert.trigger_count == 1
    assert alert.status is AlertStatus.COOLDOWN
    # 已经在上方持续时不再重复触发（没有新的上升沿）。
    provider.level.status = KeyLevelStatus.BROKEN
    engine.handle_event(_kline(BASE_TS + 3 * MINUTE, "101.5", high="102", low="101"))
    assert alert.trigger_count == 1


def test_break_requires_latest_status_to_be_broken_or_flipped() -> None:
    provider = LevelProvider(_level())
    engine, _, _ = _engine(provider)
    alert = _level_alert(AlertLevelEvent.BREAK)
    _put(engine, alert)

    engine.handle_event(_kline(BASE_TS, "100"))
    # 收盘已在上方，但关键位仍报告 ACTIVE：以最新状态为准，不触发。
    engine.handle_event(_kline(BASE_TS + MINUTE, "101", high="101", low="100"))
    assert alert.trigger_count == 0

    provider.level.status = KeyLevelStatus.FLIPPED
    engine.handle_event(_kline(BASE_TS + 2 * MINUTE, "99", high="100", low="99"))
    # FLIPPED 后方向语义翻转：向下穿越视为有效突破。
    assert alert.trigger_count == 1


def test_retest_needs_pullback_close_back_on_breakout_side() -> None:
    provider = LevelProvider(_level())
    engine, _, _ = _engine(provider)
    alert = _level_alert(AlertLevelEvent.RETEST, cooldown_ms=0)
    _put(engine, alert)

    engine.handle_event(_kline(BASE_TS, "100"))
    provider.level.status = KeyLevelStatus.BROKEN
    # 向上突破；突破柱本身不算回踩。
    engine.handle_event(_kline(BASE_TS + MINUTE, "102", high="102", low="101"))
    assert alert.trigger_count == 0
    # 回踩到关键位并收盘回到上方：触发。
    engine.handle_event(_kline(BASE_TS + 2 * MINUTE, "100.6", high="101", low="99.5"))
    assert alert.trigger_count == 1
    # 同一段突破-回踩序列只触发一次（cooldown=0 也不重复）。
    engine.handle_event(_kline(BASE_TS + 3 * MINUTE, "100.7", high="101", low="99.5"))
    assert alert.trigger_count == 1


def test_flipped_status_reverses_retest_direction() -> None:
    provider = LevelProvider(_level())
    engine, _, _ = _engine(provider)
    alert = _level_alert(AlertLevelEvent.RETEST, cooldown_ms=0)
    _put(engine, alert)

    engine.handle_event(_kline(BASE_TS, "100"))
    provider.level.status = KeyLevelStatus.BROKEN
    engine.handle_event(_kline(BASE_TS + MINUTE, "98", high="99", low="98"))
    assert alert.trigger_count == 0
    # 价格收回上方，关键位 FLIPPED：方向由下翻转向上。
    provider.level.status = KeyLevelStatus.FLIPPED
    engine.handle_event(_kline(BASE_TS + 2 * MINUTE, "101", high="101", low="99"))
    assert alert.trigger_count == 0
    # 回踩 100 并收盘回到上方：按翻转后的方向触发。
    engine.handle_event(_kline(BASE_TS + 3 * MINUTE, "100.8", high="101", low="99.5"))
    assert alert.trigger_count == 1


def test_single_shot_expires_after_first_trigger() -> None:
    provider = LevelProvider(_level())
    engine, _, _ = _engine(provider)
    alert = _level_alert(AlertLevelEvent.TOUCH, repeat=False)
    _put(engine, alert)

    engine.handle_event(_kline(BASE_TS, "105"))
    engine.handle_event(_kline(BASE_TS + MINUTE, "100.5", high="101", low="99"))
    assert alert.trigger_count == 1
    assert alert.status is AlertStatus.EXPIRED
    engine.handle_event(_kline(BASE_TS + 2 * MINUTE, "100.5", high="101", low="99"))
    assert alert.trigger_count == 1


def test_stale_feed_suspends_and_recovery_resets_baseline() -> None:
    provider = LevelProvider(_level())
    engine, _, _ = _engine(provider)
    alert = _level_alert(AlertLevelEvent.TOUCH)
    _put(engine, alert)

    engine.handle_event(_kline(BASE_TS, "105"))
    engine.handle_event(_health(FeedHealthStatus.STALE))
    engine.handle_event(_kline(BASE_TS + MINUTE, "100.5", high="101", low="99"))
    assert alert.trigger_count == 0

    engine.handle_event(_health(FeedHealthStatus.LIVE, BASE_TS + MINUTE))
    # 恢复后的旧事件被跳过；随后新柱重建基线，不立即触发。
    engine.handle_event(_kline(BASE_TS + 2 * MINUTE, "100.5", high="101", low="99"))
    assert alert.trigger_count == 0
    engine.handle_event(_kline(BASE_TS + 3 * MINUTE, "105"))
    engine.handle_event(_kline(BASE_TS + 4 * MINUTE, "100.4", high="101", low="99"))
    assert alert.trigger_count == 1


def test_provider_receives_only_available_cutoff() -> None:
    provider = LevelProvider(_level())
    engine, _, _ = _engine(provider)
    alert = _level_alert(AlertLevelEvent.TOUCH)
    _put(engine, alert)

    engine.handle_event(_kline(BASE_TS, "105", confirmed=True))
    assert provider.calls[-1] == (INSTRUMENT, TIMEFRAME, LEVEL_ID, BASE_TS + MINUTE)
    # 未确认柱：cutoff 停在该柱开盘时刻，形成中的柱对关键位不可见。
    engine.handle_event(_kline(BASE_TS + MINUTE, "100.5", high="101", low="99", confirmed=False))
    assert provider.calls[-1][3] == BASE_TS + MINUTE
    assert all(call[3] <= BASE_TS + MINUTE for call in provider.calls)


def test_level_kind_mismatch_is_ignored() -> None:
    provider = LevelProvider(_level(kind=KeyLevelKind.PREV_DAY_HIGH))
    engine, _, _ = _engine(provider)
    alert = _level_alert(AlertLevelEvent.TOUCH, level_kind=KeyLevelKind.ROUND_NUMBER)
    _put(engine, alert)

    engine.handle_event(_kline(BASE_TS, "105"))
    engine.handle_event(_kline(BASE_TS + MINUTE, "100.5", high="101", low="99"))
    assert alert.trigger_count == 0


def test_zone_touch_uses_low_and_high() -> None:
    provider = LevelProvider(_level(low="100", high="102", kind=KeyLevelKind.SESSION_HIGH))
    engine, _, _ = _engine(provider)
    alert = _level_alert(AlertLevelEvent.TOUCH)
    _put(engine, alert)

    engine.handle_event(_kline(BASE_TS, "105"))
    engine.handle_event(_kline(BASE_TS + MINUTE, "101", high="101", low="99"))
    assert alert.trigger_count == 1


def test_level_event_contract_rejects_cross_kind_fields() -> None:
    with pytest.raises(ValueError):
        AlertCondition(
            kind=AlertConditionKind.LEVEL_EVENT,
            level_id=LEVEL_ID,
            level_event=AlertLevelEvent.TOUCH,
            threshold=Decimal("100"),
        )
    with pytest.raises(ValueError):
        AlertCondition(kind=AlertConditionKind.LEVEL_EVENT, level_id=LEVEL_ID)
    with pytest.raises(ValueError):
        AlertCondition(
            kind=AlertConditionKind.METRIC_THRESHOLD,
            direction="ABOVE",
            metric="last_price",
            threshold=Decimal("100"),
            level_id=LEVEL_ID,
            level_event=AlertLevelEvent.TOUCH,
        )
