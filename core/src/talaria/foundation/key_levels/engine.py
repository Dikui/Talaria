"""无前视关键位引擎：只使用 ``ts_available <= cutoff_ts`` 的已确认 K 线。

核心只依赖 foundation：市场包通过 ``SessionTrait`` 注入时区、日切与时段窗口，
这里不出现任何交易所或资产类别分支。所有函数均为纯函数，输出顺序确定。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal, ROUND_FLOOR
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from talaria.foundation.bars import BarStatus, CompactBar
from talaria.foundation.key_levels.contracts import (
    KEY_LEVEL_METHOD_VERSION,
    KIND_CATEGORY,
    KeyLevel,
    KeyLevelCategory,
    KeyLevelKind,
    KeyLevelParams,
    KeyLevelStatus,
    SessionProfile,
)
from talaria.foundation.timeframes import timeframe_ms

_MS = 1000
_QUANTUM = Decimal("0.000001")
MAX_STRENGTH = 0.95


@dataclass(frozen=True, slots=True)
class _Pivot:
    side: KeyLevelKind
    price: Decimal
    index: int
    atr: Decimal | None
    prominence: Decimal
    ts_available: int
    formed_ts: int


@dataclass(frozen=True, slots=True)
class _TouchStats:
    count: int
    last_ts: int | None
    volume_weight: Decimal


def _normalize(price: Decimal) -> str:
    normalized = price.normalize()
    text = format(normalized, "f")
    return "0" if text in {"-0", ""} else text


def _level_id(
    instrument_id: str,
    kind: KeyLevelKind,
    timeframe: str,
    anchor_ts: int,
    anchor_price: Decimal,
    tag: str = "",
) -> str:
    payload = "|".join(
        (
            instrument_id,
            kind.value,
            timeframe,
            str(anchor_ts),
            _normalize(anchor_price),
            tag,
        )
    )
    return "kl_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _strength(value: float) -> float:
    return round(max(0.05, min(MAX_STRENGTH, value)), 3)


def _decimal_volume_weight(part: Decimal, total: Decimal) -> Decimal:
    if total <= 0:
        return Decimal("0")
    return (part / total).quantize(_QUANTUM)


def _zone_tolerance(atr: Decimal | None, price: Decimal, ratio: float) -> Decimal:
    if atr is not None and atr > 0:
        return atr * Decimal(str(ratio))
    return max(price * Decimal("0.0005"), _QUANTUM)


def _true_ranges(bars: list[CompactBar]) -> list[Decimal | None]:
    ranges: list[Decimal | None] = []
    previous_close: Decimal | None = None
    for bar in bars:
        if bar.high is None or bar.low is None or bar.close is None:
            ranges.append(None)
            previous_close = None
            continue
        if previous_close is None:
            ranges.append(bar.high - bar.low)
        else:
            ranges.append(
                max(bar.high, previous_close) - min(bar.low, previous_close)
            )
        previous_close = bar.close
    return ranges


def _atr_series(bars: list[CompactBar], period: int) -> list[Decimal | None]:
    ranges = _true_ranges(bars)
    result: list[Decimal | None] = [None] * len(bars)
    for index in range(len(bars)):
        window = ranges[max(0, index - period + 1) : index + 1]
        if any(item is None for item in window):
            continue
        values = [item for item in window if item is not None]
        result[index] = sum(values, Decimal("0")) / Decimal(len(values))
    return result


def _tise(bar: CompactBar) -> Decimal | None:
    return bar.volume


def _touch_stats(
    bars: list[CompactBar], start_index: int, low: Decimal, high: Decimal, tolerance: Decimal
) -> _TouchStats:
    count = 0
    last_ts: int | None = None
    touched_volume = Decimal("0")
    total_volume = Decimal("0")
    for bar in bars[start_index:]:
        if bar.status is BarStatus.FEED_GAP or bar.high is None or bar.low is None:
            continue
        volume = _tise(bar) or Decimal("0")
        total_volume += volume
        if bar.high >= low - tolerance and bar.low <= high + tolerance:
            count += 1
            last_ts = bar.timestamp
            touched_volume += volume
    return _TouchStats(
        count=count,
        last_ts=last_ts,
        volume_weight=_decimal_volume_weight(touched_volume, total_volume),
    )


def _status(
    bars: list[CompactBar],
    low: Decimal,
    high: Decimal,
    ts_available: int,
) -> KeyLevelStatus:
    after = [bar for bar in bars if bar.ts_available > ts_available]
    if not after:
        return KeyLevelStatus.ACTIVE
    direction: str | None = None
    for bar in after:
        if bar.close is None:
            continue
        if bar.close > high:
            direction = "up"
            break
        if bar.close < low:
            direction = "down"
            break
    if direction is None:
        return KeyLevelStatus.ACTIVE
    last_close = next(
        (bar.close for bar in reversed(after) if bar.close is not None), None
    )
    if last_close is None:
        return KeyLevelStatus.ACTIVE
    if low <= last_close <= high:
        return KeyLevelStatus.ACTIVE
    if direction == "up":
        return KeyLevelStatus.BROKEN if last_close > high else KeyLevelStatus.FLIPPED
    return KeyLevelStatus.BROKEN if last_close < low else KeyLevelStatus.FLIPPED


def _recency(cutoff_ts: int, last_ts: int | None, bar_ms: int) -> float:
    if last_ts is None or bar_ms <= 0:
        return 0.0
    age = max(0, cutoff_ts - last_ts) / (bar_ms * 200)
    return max(0.0, 1.0 - age)


def _parameters(params: KeyLevelParams, **extra: str) -> dict[str, str]:
    merged = params.as_parameters()
    merged.update(extra)
    return merged


def _bar_ms(timeframe: str) -> int:
    try:
        return timeframe_ms(timeframe)
    except KeyError:
        return 60_000


def _structure_levels(
    *,
    instrument_id: str,
    timeframe: str,
    bars: list[CompactBar],
    cutoff_ts: int,
    atr: list[Decimal | None],
    params: KeyLevelParams,
    bar_ms: int,
) -> list[KeyLevel]:
    span = params.swing_span
    confirm_offset = max(span, params.confirm_bars)
    highs = [bar.high for bar in bars]
    lows = [bar.low for bar in bars]
    pivots: list[_Pivot] = []
    for index in range(span, len(bars) - span):
        confirm_index = index + confirm_offset
        if confirm_index >= len(bars):
            continue
        confirm_bar = bars[confirm_index]
        if confirm_bar.ts_available > cutoff_ts:
            continue
        high = highs[index]
        low = lows[index]
        if high is None or low is None:
            continue
        window_high = max(
            (highs[position] for position in range(index - span, index + span + 1) if highs[position] is not None),
            default=None,
        )
        window_low = min(
            (lows[position] for position in range(index - span, index + span + 1) if lows[position] is not None),
            default=None,
        )
        local_atr = atr[index]
        if window_high is not None and high == window_high:
            neighbours = [
                highs[position]
                for position in (*range(index - span, index), *range(index + 1, index + span + 1))
                if highs[position] is not None
            ]
            if neighbours:
                prominence = high - max(neighbours)
                threshold = (local_atr or Decimal("0")) * Decimal(
                    str(params.pivot_atr_min)
                )
                if prominence >= threshold:
                    pivots.append(
                        _Pivot(
                            side=KeyLevelKind.SWING_HIGH,
                            price=high,
                            index=index,
                            atr=local_atr,
                            prominence=prominence,
                            ts_available=confirm_bar.ts_available,
                            formed_ts=bars[index].timestamp,
                        )
                    )
        if window_low is not None and low == window_low:
            neighbours = [
                lows[position]
                for position in (*range(index - span, index), *range(index + 1, index + span + 1))
                if lows[position] is not None
            ]
            if neighbours:
                prominence = min(neighbours) - low
                threshold = (local_atr or Decimal("0")) * Decimal(
                    str(params.pivot_atr_min)
                )
                if prominence >= threshold:
                    pivots.append(
                        _Pivot(
                            side=KeyLevelKind.SWING_LOW,
                            price=low,
                            index=index,
                            atr=local_atr,
                            prominence=prominence,
                            ts_available=confirm_bar.ts_available,
                            formed_ts=bars[index].timestamp,
                        )
                    )

    levels: list[KeyLevel] = []
    for side in (KeyLevelKind.SWING_HIGH, KeyLevelKind.SWING_LOW):
        side_pivots = sorted(
            (pivot for pivot in pivots if pivot.side is side),
            key=lambda pivot: (pivot.price, pivot.index),
        )
        clusters: list[list[_Pivot]] = []
        for pivot in side_pivots:
            target: list[_Pivot] | None = None
            for cluster in clusters:
                tolerance = max(
                    _zone_tolerance(member.atr, member.price, params.cluster_atr)
                    for member in (*cluster, pivot)
                )
                if pivot.price - max(member.price for member in cluster) <= tolerance:
                    target = cluster
                    break
            if target is None:
                clusters.append([pivot])
            else:
                target.append(pivot)

        for cluster in clusters:
            earliest = min(cluster, key=lambda pivot: pivot.index)
            lowest = min(pivot.price for pivot in cluster)
            highest = max(pivot.price for pivot in cluster)
            zone_low, zone_high = lowest, highest
            tolerance = max(
                _zone_tolerance(pivot.atr, pivot.price, params.touch_atr)
                for pivot in cluster
            )
            stats = _touch_stats(
                bars, earliest.index, zone_low, zone_high, tolerance
            )
            quality = 0.0
            for pivot in cluster:
                if pivot.atr and pivot.atr > 0:
                    quality = max(
                        quality, float(min(Decimal("1"), pivot.prominence / pivot.atr))
                    )
            last_ts = stats.last_ts if stats.last_ts is not None else earliest.formed_ts
            strength = (
                0.28
                + 0.08 * min(stats.count, 6)
                + 0.22 * float(stats.volume_weight)
                + 0.12 * _recency(cutoff_ts, last_ts, bar_ms)
                + 0.15 * quality
            )
            ts_available = max(pivot.ts_available for pivot in cluster)
            single = zone_low == zone_high
            level = KeyLevel(
                level_id=_level_id(
                    instrument_id,
                    side,
                    timeframe,
                    earliest.formed_ts,
                    earliest.price,
                ),
                instrument_id=instrument_id,
                kind=side,
                category=KIND_CATEGORY[side],
                timeframe=timeframe,
                price=zone_low if single else None,
                price_low=None if single else zone_low,
                price_high=None if single else zone_high,
                strength=_strength(strength),
                status=_status(bars, zone_low, zone_high, ts_available),
                ts_available=ts_available,
                formed_ts=earliest.formed_ts,
                method_version=KEY_LEVEL_METHOD_VERSION,
                parameters=_parameters(params),
                touch_count=stats.count,
                last_touch_ts=stats.last_ts,
                volume_weight=stats.volume_weight,
            )
            levels.append(level)
    return levels


def _timezone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def _parse_hm(value: str) -> tuple[int, int]:
    hour_text, _, minute_text = value.partition(":")
    return int(hour_text or 0), int(minute_text or 0)


def _boundary_ms(day: date, tz: ZoneInfo, day_start: str) -> int:
    hour, minute = _parse_hm(day_start)
    moment = datetime.combine(day, time(hour, minute), tzinfo=tz)
    return int(moment.timestamp() * _MS)


def _day_start_ms(ts: int, tz: ZoneInfo, day_start: str) -> int:
    moment = datetime.fromtimestamp(ts / _MS, tz)
    hour, minute = _parse_hm(day_start)
    boundary = moment.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if moment < boundary:
        boundary -= timedelta(days=1)
    return int(boundary.timestamp() * _MS)


def _previous_day_start(day_start_ms: int, tz: ZoneInfo) -> int:
    moment = datetime.fromtimestamp(day_start_ms / _MS, tz)
    return int((moment - timedelta(days=1)).timestamp() * _MS)


def _window(bars: list[CompactBar], start_ms: int, end_ms: int) -> list[CompactBar]:
    return [bar for bar in bars if start_ms <= bar.timestamp < end_ms]


def _window_complete(
    window: list[CompactBar], start_ms: int, end_ms: int, bar_ms: int
) -> bool:
    if not window:
        return False
    if window[0].timestamp - start_ms > bar_ms * Decimal("1.5"):
        return False
    return window[-1].ts_available >= end_ms - bar_ms


def _window_stats(
    bars: list[CompactBar], start_index: int, low: Decimal, high: Decimal, tolerance: Decimal
) -> _TouchStats:
    return _touch_stats(bars, start_index, low, high, tolerance)


def _session_level(
    *,
    instrument_id: str,
    timeframe: str,
    bars: list[CompactBar],
    cutoff_ts: int,
    params: KeyLevelParams,
    bar_ms: int,
    kind: KeyLevelKind,
    price: Decimal,
    formed_ts: int,
    ts_available: int,
    anchor_ts: int,
    tag: str,
    parameters: dict[str, str],
    strength: float,
    touch_index: int,
    zone_low: Decimal | None = None,
    zone_high: Decimal | None = None,
) -> KeyLevel:
    low = zone_low if zone_low is not None else price
    high = zone_high if zone_high is not None else price
    tolerance = _zone_tolerance(None, price, params.touch_atr)
    stats = _window_stats(bars, touch_index, low, high, tolerance)
    last_ts = stats.last_ts if stats.last_ts is not None else formed_ts
    final_strength = strength + 0.08 * min(stats.count, 4) + 0.12 * float(
        stats.volume_weight
    ) + 0.08 * _recency(cutoff_ts, last_ts, bar_ms)
    single = zone_low is None or zone_high is None
    return KeyLevel(
        level_id=_level_id(instrument_id, kind, timeframe, anchor_ts, price, tag),
        instrument_id=instrument_id,
        kind=kind,
        category=KIND_CATEGORY[kind],
        timeframe=timeframe,
        price=price if single else None,
        price_low=None if single else zone_low,
        price_high=None if single else zone_high,
        strength=_strength(final_strength),
        status=_status(bars, low, high, ts_available),
        ts_available=ts_available,
        formed_ts=formed_ts,
        method_version=KEY_LEVEL_METHOD_VERSION,
        parameters=parameters,
        touch_count=stats.count,
        last_touch_ts=stats.last_ts,
        volume_weight=stats.volume_weight,
    )


def _session_levels(
    *,
    instrument_id: str,
    timeframe: str,
    bars: list[CompactBar],
    cutoff_ts: int,
    params: KeyLevelParams,
    bar_ms: int,
    profile: SessionProfile,
) -> list[KeyLevel]:
    tz = _timezone(profile.timezone)
    current_day = _day_start_ms(cutoff_ts, tz, profile.day_start)
    previous_day = _previous_day_start(current_day, tz)
    previous_window = _window(bars, previous_day, current_day)
    levels: list[KeyLevel] = []
    if _window_complete(previous_window, previous_day, current_day, bar_ms):
        usable = [
            bar for bar in previous_window if bar.status is not BarStatus.FEED_GAP
        ]
        if usable:
            low_price = min(bar.low for bar in usable if bar.low is not None)
            high_price = max(bar.high for bar in usable if bar.high is not None)
            close_price = next(
                (bar.close for bar in reversed(usable) if bar.close is not None), None
            )
            first_index = bars.index(usable[0])
            common = _parameters(
                params, timezone=profile.timezone, day_start=profile.day_start
            )
            for kind, price in (
                (KeyLevelKind.PREV_DAY_HIGH, high_price),
                (KeyLevelKind.PREV_DAY_LOW, low_price),
            ):
                if price is None:
                    continue
                levels.append(
                    _session_level(
                        instrument_id=instrument_id,
                        timeframe=timeframe,
                        bars=bars,
                        cutoff_ts=cutoff_ts,
                        params=params,
                        bar_ms=bar_ms,
                        kind=kind,
                        price=price,
                        formed_ts=previous_day,
                        ts_available=current_day,
                        anchor_ts=previous_day,
                        tag=profile.day_start,
                        parameters=dict(common),
                        strength=0.4,
                        touch_index=first_index,
                    )
                )
            if close_price is not None:
                levels.append(
                    _session_level(
                        instrument_id=instrument_id,
                        timeframe=timeframe,
                        bars=bars,
                        cutoff_ts=cutoff_ts,
                        params=params,
                        bar_ms=bar_ms,
                        kind=KeyLevelKind.PREV_DAY_CLOSE,
                        price=close_price,
                        formed_ts=previous_day,
                        ts_available=current_day,
                        anchor_ts=previous_day,
                        tag=profile.day_start,
                        parameters=dict(common),
                        strength=0.36,
                        touch_index=first_index,
                    )
                )

    month_start = _month_start_ms(cutoff_ts, tz, profile.day_start)
    month_window = _window(bars, month_start, cutoff_ts + 1)
    if month_window and month_window[0].timestamp - month_start <= bar_ms * Decimal("1.5"):
        first = month_window[0]
        if first.open is not None:
            levels.append(
                _session_level(
                    instrument_id=instrument_id,
                    timeframe=timeframe,
                    bars=bars,
                    cutoff_ts=cutoff_ts,
                    params=params,
                    bar_ms=bar_ms,
                    kind=KeyLevelKind.MONTH_OPEN,
                    price=first.open,
                    formed_ts=first.timestamp,
                    ts_available=first.ts_available,
                    anchor_ts=month_start,
                    tag="month",
                    parameters=_parameters(params, timezone=profile.timezone),
                    strength=0.34,
                    touch_index=bars.index(first),
                )
            )

    week_start = _week_start_ms(cutoff_ts, tz, profile.day_start)
    week_window = _window(bars, week_start, cutoff_ts + 1)
    if week_window and week_window[0].timestamp - week_start <= bar_ms * Decimal("1.5"):
        first = week_window[0]
        if first.open is not None:
            levels.append(
                _session_level(
                    instrument_id=instrument_id,
                    timeframe=timeframe,
                    bars=bars,
                    cutoff_ts=cutoff_ts,
                    params=params,
                    bar_ms=bar_ms,
                    kind=KeyLevelKind.WEEK_OPEN,
                    price=first.open,
                    formed_ts=first.timestamp,
                    ts_available=first.ts_available,
                    anchor_ts=week_start,
                    tag="week",
                    parameters=_parameters(params, timezone=profile.timezone),
                    strength=0.4,
                    touch_index=bars.index(first),
                )
            )

    range_ms = params.opening_range_minutes * 60 * _MS
    if bar_ms <= range_ms:
        range_end = current_day + range_ms
        if cutoff_ts >= range_end:
            range_window = _window(bars, current_day, range_end)
            if _window_complete(range_window, current_day, range_end, bar_ms):
                usable = [
                    bar
                    for bar in range_window
                    if bar.status is not BarStatus.FEED_GAP
                ]
                if usable:
                    high_price = max(bar.high for bar in usable if bar.high is not None)
                    low_price = min(bar.low for bar in usable if bar.low is not None)
                    first_index = bars.index(usable[0])
                    if high_price != low_price:
                        levels.append(
                            _session_level(
                                instrument_id=instrument_id,
                                timeframe=timeframe,
                                bars=bars,
                                cutoff_ts=cutoff_ts,
                                params=params,
                                bar_ms=bar_ms,
                                kind=KeyLevelKind.OPENING_RANGE,
                                price=low_price,
                                formed_ts=current_day,
                                ts_available=range_end,
                                anchor_ts=current_day,
                                tag=str(params.opening_range_minutes),
                                parameters=_parameters(
                                    params, minutes=str(params.opening_range_minutes)
                                ),
                                strength=0.38,
                                touch_index=first_index,
                                zone_low=low_price,
                                zone_high=high_price,
                            )
                        )

    if profile.sessions and bar_ms <= 60 * 60 * _MS:
        for session in profile.sessions:
            session_tz = _timezone(session.timezone or profile.timezone)
            window = _latest_session_window(
                cutoff_ts, session.start, session.end, session_tz
            )
            if window is None:
                continue
            start_ms, end_ms = window
            session_window = _window(bars, start_ms, end_ms)
            if not _window_complete(session_window, start_ms, end_ms, bar_ms):
                continue
            usable = [
                bar for bar in session_window if bar.status is not BarStatus.FEED_GAP
            ]
            if not usable:
                continue
            high_price = max(bar.high for bar in usable if bar.high is not None)
            low_price = min(bar.low for bar in usable if bar.low is not None)
            first_index = bars.index(usable[0])
            common = _parameters(
                params,
                session=session.name,
                timezone=session.timezone or profile.timezone,
            )
            for kind, price in (
                (KeyLevelKind.SESSION_HIGH, high_price),
                (KeyLevelKind.SESSION_LOW, low_price),
            ):
                levels.append(
                    _session_level(
                        instrument_id=instrument_id,
                        timeframe=timeframe,
                        bars=bars,
                        cutoff_ts=cutoff_ts,
                        params=params,
                        bar_ms=bar_ms,
                        kind=kind,
                        price=price,
                        formed_ts=start_ms,
                        ts_available=end_ms,
                        anchor_ts=start_ms,
                        tag=session.name,
                        parameters=dict(common),
                        strength=0.34,
                        touch_index=first_index,
                    )
                )
    return levels


def _month_start_ms(ts: int, tz: ZoneInfo, day_start: str) -> int:
    day = datetime.fromtimestamp(ts / _MS, tz).date()
    return _boundary_ms(day.replace(day=1), tz, day_start)


def _week_start_ms(ts: int, tz: ZoneInfo, day_start: str) -> int:
    day = datetime.fromtimestamp(ts / _MS, tz).date()
    monday = day - timedelta(days=day.weekday())
    return _boundary_ms(monday, tz, day_start)


def _session_window_for_day(
    day: date, start: str, end: str, tz: ZoneInfo
) -> tuple[int, int]:
    start_hour, start_minute = _parse_hm(start)
    end_hour, end_minute = _parse_hm(end)
    start_dt = datetime.combine(day, time(start_hour, start_minute), tzinfo=tz)
    end_dt = datetime.combine(day, time(end_hour, end_minute), tzinfo=tz)
    if end_dt <= start_dt:
        end_dt += timedelta(days=1)
    return int(start_dt.timestamp() * _MS), int(end_dt.timestamp() * _MS)


def _latest_session_window(
    cutoff_ts: int, start: str, end: str, tz: ZoneInfo
) -> tuple[int, int] | None:
    today = datetime.fromtimestamp(cutoff_ts / _MS, tz).date()
    for offset in (0, 1, 2):
        candidate = today - timedelta(days=offset)
        start_ms, end_ms = _session_window_for_day(candidate, start, end, tz)
        if end_ms <= cutoff_ts:
            return start_ms, end_ms
    return None


def _gap_levels(
    *,
    instrument_id: str,
    timeframe: str,
    bars: list[CompactBar],
    cutoff_ts: int,
    atr: list[Decimal | None],
    params: KeyLevelParams,
    bar_ms: int,
) -> list[KeyLevel]:
    levels: list[KeyLevel] = []
    for index in range(1, len(bars)):
        current = bars[index]
        previous = bars[index - 1]
        if (
            current.high is None
            or current.low is None
            or previous.high is None
            or previous.low is None
        ):
            continue
        direction: str | None = None
        zone_low: Decimal | None = None
        zone_high: Decimal | None = None
        if current.low > previous.high:
            direction = "up"
            zone_low, zone_high = previous.high, current.low
        elif current.high < previous.low:
            direction = "down"
            zone_low, zone_high = current.high, previous.low
        if direction is None or zone_low is None or zone_high is None:
            continue
        local_atr = atr[index]
        size = zone_high - zone_low
        if local_atr is not None and size < local_atr * Decimal(str(params.gap_atr_min)):
            continue
        filled = False
        for later in bars[index + 1 :]:
            if later.high is None or later.low is None:
                continue
            if direction == "up" and later.low <= zone_low:
                filled = True
                break
            if direction == "down" and later.high >= zone_high:
                filled = True
                break
        if filled:
            continue
        tolerance = _zone_tolerance(local_atr, current.close or zone_high, params.touch_atr)
        stats = _touch_stats(bars, index, zone_low, zone_high, tolerance)
        last_ts = stats.last_ts if stats.last_ts is not None else current.timestamp
        size_score = 1.0
        if local_atr and local_atr > 0:
            size_score = float(min(Decimal("1"), size / (local_atr * Decimal("3"))))
        strength = (
            0.34
            + 0.2 * size_score
            + 0.1 * float(stats.volume_weight)
            + 0.12 * _recency(cutoff_ts, last_ts, bar_ms)
            + 0.06
        )
        levels.append(
            KeyLevel(
                level_id=_level_id(
                    instrument_id,
                    KeyLevelKind.GAP,
                    timeframe,
                    current.timestamp,
                    zone_low,
                    direction,
                ),
                instrument_id=instrument_id,
                kind=KeyLevelKind.GAP,
                category=KIND_CATEGORY[KeyLevelKind.GAP],
                timeframe=timeframe,
                price=None,
                price_low=zone_low,
                price_high=zone_high,
                strength=_strength(strength),
                status=_status(bars, zone_low, zone_high, current.ts_available),
                ts_available=current.ts_available,
                formed_ts=current.timestamp,
                method_version=KEY_LEVEL_METHOD_VERSION,
                parameters=_parameters(params, direction=direction),
                touch_count=stats.count,
                last_touch_ts=stats.last_ts,
                volume_weight=stats.volume_weight,
            )
        )
    return levels


def _round_step(price: Decimal) -> Decimal:
    digits = price.adjusted() + 1
    exponent = max(-6, min(6, digits - 2))
    return Decimal(10) ** exponent


def _round_levels(
    *,
    instrument_id: str,
    timeframe: str,
    bars: list[CompactBar],
    cutoff_ts: int,
    atr: list[Decimal | None],
    params: KeyLevelParams,
    bar_ms: int,
) -> list[KeyLevel]:
    last = bars[-1]
    if last.close is None or last.close <= 0:
        return []
    step = params.round_step or _round_step(last.close)
    if step <= 0:
        return []
    base = (last.close / step).to_integral_value(rounding=ROUND_FLOOR) * step
    local_atr = atr[-1]
    window = (
        local_atr * Decimal(str(params.round_window_atr))
        if local_atr is not None and local_atr > 0
        else last.close * Decimal("0.02")
    )
    levels: list[KeyLevel] = []
    for offset in range(-params.round_levels_per_side, params.round_levels_per_side + 1):
        price = base + step * offset
        if price <= 0:
            continue
        distance = abs(price - last.close)
        if distance > window:
            continue
        tolerance = _zone_tolerance(local_atr, price, params.touch_atr)
        stats = _touch_stats(bars, 0, price, price, tolerance)
        proximity = 1.0 - float(distance / window) if window > 0 else 0.0
        strength = (
            0.24
            + 0.3 * proximity
            + 0.05 * min(stats.count, 5)
            + 0.1 * float(stats.volume_weight)
        )
        levels.append(
            KeyLevel(
                level_id=_level_id(
                    instrument_id,
                    KeyLevelKind.ROUND_NUMBER,
                    timeframe,
                    0,
                    price,
                    _normalize(step),
                ),
                instrument_id=instrument_id,
                kind=KeyLevelKind.ROUND_NUMBER,
                category=KIND_CATEGORY[KeyLevelKind.ROUND_NUMBER],
                timeframe=timeframe,
                price=price,
                strength=_strength(strength),
                status=KeyLevelStatus.ACTIVE,
                ts_available=last.ts_available,
                formed_ts=last.timestamp,
                method_version=KEY_LEVEL_METHOD_VERSION,
                parameters=_parameters(params, step=_normalize(step)),
                touch_count=stats.count,
                last_touch_ts=stats.last_ts,
                volume_weight=stats.volume_weight,
            )
        )
    return levels


def compute_key_levels(
    *,
    instrument_id: str,
    timeframe: str,
    bars: list[CompactBar],
    cutoff_ts: int,
    session_profile: SessionProfile | None = None,
    params: KeyLevelParams | None = None,
) -> list[KeyLevel]:
    """计算截止 ``cutoff_ts`` 已可见的全部关键位。

    输入可以包含未来柱；引擎只保留 ``ts_available <= cutoff_ts`` 的柱子，并在
    枢轴确认柱闭合后才产出对应结构位，因此任何返回项都不会泄露未来信息。
    """

    params = params or KeyLevelParams()
    profile = session_profile or SessionProfile()
    ordered = sorted(
        (bar for bar in bars if bar.status is not BarStatus.FEED_GAP),
        key=lambda bar: bar.timestamp,
    )
    visible = [bar for bar in ordered if bar.ts_available <= cutoff_ts]
    if not visible:
        return []
    bar_ms = _bar_ms(timeframe)
    atr = _atr_series(visible, params.atr_period)

    collected: list[KeyLevel] = []
    collected.extend(
        _structure_levels(
            instrument_id=instrument_id,
            timeframe=timeframe,
            bars=visible,
            cutoff_ts=cutoff_ts,
            atr=atr,
            params=params,
            bar_ms=bar_ms,
        )
    )
    collected.extend(
        _session_levels(
            instrument_id=instrument_id,
            timeframe=timeframe,
            bars=visible,
            cutoff_ts=cutoff_ts,
            params=params,
            bar_ms=bar_ms,
            profile=profile,
        )
    )
    collected.extend(
        _gap_levels(
            instrument_id=instrument_id,
            timeframe=timeframe,
            bars=visible,
            cutoff_ts=cutoff_ts,
            atr=atr,
            params=params,
            bar_ms=bar_ms,
        )
    )
    collected.extend(
        _round_levels(
            instrument_id=instrument_id,
            timeframe=timeframe,
            bars=visible,
            cutoff_ts=cutoff_ts,
            atr=atr,
            params=params,
            bar_ms=bar_ms,
        )
    )

    deduped: dict[str, KeyLevel] = {}
    for level in collected:
        deduped.setdefault(level.level_id, level)
    ordered_levels = sorted(
        deduped.values(),
        key=lambda level: (-level.strength, -level.ts_available, level.level_id),
    )
    return ordered_levels[: params.max_levels]
