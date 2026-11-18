"""Catalyst 日历的最小防前视实现。

本单不接真实日历；空数据时返回空列表。关键语义：
- 只输出 ``schedule_available_ts <= cutoff_ts`` 的事件；
- 实际公布时点 ``actual_available_ts`` 晚于 cutoff 时，状态必须是 ``UNRELEASED``。

真实日历接入属于后续任务单，本模块保持数据注入式，便于测试。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Sequence


class CatalystStatus(StrEnum):
    RELEASED = "RELEASED"
    UNRELEASED = "UNRELEASED"


@dataclass(frozen=True, slots=True)
class CatalystEvent:
    event_id: str
    instrument_id: str | None
    schedule_available_ts: int
    actual_available_ts: int | None
    title: str = ""


@dataclass(frozen=True, slots=True)
class CatalystObservation:
    event_id: str
    instrument_id: str | None
    title: str
    status: CatalystStatus
    effective_available_ts: int | None


class CatalystCalendar:
    """把注入的事件按 cutoff 裁剪成可观察事件。"""

    def __init__(self, events: Sequence[CatalystEvent] = ()) -> None:
        self._events = tuple(events)

    def events_for(
        self,
        *,
        cutoff_ts: int,
        instrument_id: str | None = None,
    ) -> list[CatalystObservation]:
        observations: list[CatalystObservation] = []
        for event in self._events:
            if event.schedule_available_ts > cutoff_ts:
                continue
            if instrument_id is not None and event.instrument_id not in (None, instrument_id):
                continue
            released = (
                event.actual_available_ts is not None
                and event.actual_available_ts <= cutoff_ts
            )
            observations.append(
                CatalystObservation(
                    event_id=event.event_id,
                    instrument_id=event.instrument_id,
                    title=event.title,
                    status=CatalystStatus.RELEASED if released else CatalystStatus.UNRELEASED,
                    effective_available_ts=event.actual_available_ts if released else None,
                )
            )
        return observations
