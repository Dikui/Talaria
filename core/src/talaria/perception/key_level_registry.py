"""当前服务进程中冻结关键位的时点版本。"""

from __future__ import annotations

from bisect import bisect_right, insort_right
from collections.abc import Callable
from operator import attrgetter

from talaria.foundation.key_levels import KeyLevel, KeyLevelSet

_cutoff = attrgetter("cutoff_ts")


class KeyLevelRegistry:
    """按快照截止时刻选版。读写与图表投影均在服务事件循环内完成。"""

    def __init__(
        self, *, on_latest: Callable[[KeyLevelSet], None] | None = None
    ) -> None:
        self._versions: dict[tuple[str, str], list[KeyLevelSet]] = {}
        self._on_latest = on_latest

    def register(self, version: KeyLevelSet) -> None:
        key = (version.instrument_id, version.timeframe)
        versions = self._versions.setdefault(key, [])
        insort_right(versions, version, key=_cutoff)
        if versions[-1] is version and self._on_latest is not None:
            self._on_latest(version)

    def latest(self, key: tuple[str, str]) -> KeyLevelSet | None:
        versions = self._versions.get(key)
        return versions[-1] if versions else None

    def level_at(
        self, instrument_id: str, timeframe: str, level_id: str, cutoff_ts: int
    ) -> KeyLevel | None:
        versions = self._versions.get((instrument_id, timeframe))
        if not versions:
            return None
        end = bisect_right(versions, cutoff_ts, key=_cutoff)
        if end == 0:
            return None
        return next(
            (
                level for level in versions[end - 1].levels
                if level.level_id == level_id and level.ts_available <= cutoff_ts
            ),
            None,
        )
