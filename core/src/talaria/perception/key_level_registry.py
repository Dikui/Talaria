"""当前服务进程中冻结关键位的时点版本。"""

from __future__ import annotations

import logging
from bisect import bisect_right
from collections.abc import Callable
from operator import attrgetter

from talaria.foundation.key_levels import KeyLevel, KeyLevelSet

logger = logging.getLogger(__name__)
MAX_VERSIONS_PER_SERIES = 256
_cutoff = attrgetter("cutoff_ts")


class KeyLevelRegistry:
    """按快照截止时刻选版。读写与图表投影均在服务事件循环内完成。"""

    def __init__(
        self, *, now_ms: Callable[[], int], on_latest: Callable[[KeyLevelSet], None] | None = None
    ) -> None:
        self._versions: dict[tuple[str, str], list[KeyLevelSet]] = {}
        self._now_ms = now_ms
        self._on_latest = on_latest

    def register(self, version: KeyLevelSet) -> None:
        if version.cutoff_ts > self._now_ms():
            return
        key = (version.instrument_id, version.timeframe)
        versions = self._versions.setdefault(key, [])
        end = bisect_right(versions, version.cutoff_ts, key=_cutoff)
        if end and versions[end - 1].cutoff_ts == version.cutoff_ts:
            versions[end - 1] = version
        else:
            versions.insert(end, version)
            if len(versions) > MAX_VERSIONS_PER_SERIES:
                del versions[0]
        if versions[-1] is version and self._on_latest is not None:
            self._on_latest(version)

    def latest(self, key: tuple[str, str]) -> KeyLevelSet | None:
        versions = self._versions.get(key)
        return versions[-1] if versions else None

    def levels_at(
        self, instrument_id: str, timeframe: str, cutoff_ts: int
    ) -> list[KeyLevel]:
        versions = self._versions.get((instrument_id, timeframe))
        if not versions:
            return []
        end = bisect_right(versions, cutoff_ts, key=_cutoff)
        if end == 0:
            logger.debug(
                "%s %s 的事件截止时刻 %s 早于最早关键位版本 %s",
                instrument_id, timeframe, cutoff_ts, versions[0].cutoff_ts,
            )
            return []
        return [level for level in versions[end - 1].levels if level.ts_available <= cutoff_ts]
