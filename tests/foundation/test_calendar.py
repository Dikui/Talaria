"""CatalystCalendar 的防前视语义。"""

from __future__ import annotations

from talaria.foundation.calendar import (
    CatalystCalendar,
    CatalystEvent,
    CatalystStatus,
)


def test_empty_calendar_returns_nothing() -> None:
    assert CatalystCalendar().events_for(cutoff_ts=1_000) == []


def test_schedule_available_after_cutoff_is_hidden() -> None:
    calendar = CatalystCalendar(
        [
            CatalystEvent(
                event_id="future",
                instrument_id=None,
                schedule_available_ts=1_100,
                actual_available_ts=1_100,
            )
        ]
    )
    assert calendar.events_for(cutoff_ts=1_000) == []


def test_actual_after_cutoff_is_unreleased_and_has_no_effective_time() -> None:
    calendar = CatalystCalendar(
        [
            CatalystEvent(
                event_id="cpi",
                instrument_id="BTC-USDT-PERP",
                schedule_available_ts=900,
                actual_available_ts=1_050,
                title="CPI",
            )
        ]
    )
    observation = calendar.events_for(cutoff_ts=1_000)[0]
    assert observation.status is CatalystStatus.UNRELEASED
    assert observation.effective_available_ts is None


def test_released_event_uses_actual_available_time() -> None:
    calendar = CatalystCalendar(
        [
            CatalystEvent(
                event_id="cpi",
                instrument_id=None,
                schedule_available_ts=900,
                actual_available_ts=950,
            )
        ]
    )
    observation = calendar.events_for(cutoff_ts=1_000)[0]
    assert observation.status is CatalystStatus.RELEASED
    assert observation.effective_available_ts == 950


def test_missing_actual_is_unreleased() -> None:
    calendar = CatalystCalendar(
        [
            CatalystEvent(
                event_id="cpi",
                instrument_id=None,
                schedule_available_ts=900,
                actual_available_ts=None,
            )
        ]
    )
    observation = calendar.events_for(cutoff_ts=1_000)[0]
    assert observation.status is CatalystStatus.UNRELEASED


def test_instrument_filter_keeps_global_events() -> None:
    calendar = CatalystCalendar(
        [
            CatalystEvent("global", None, 900, 950),
            CatalystEvent("eth", "ETH-USDT-PERP", 900, 950),
            CatalystEvent("btc", "BTC-USDT-PERP", 900, 950),
        ]
    )
    observations = calendar.events_for(cutoff_ts=1_000, instrument_id="BTC-USDT-PERP")
    assert [item.event_id for item in observations] == ["global", "btc"]
