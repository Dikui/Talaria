"""当前服务进程中冻结关键位的时点版本。"""

from __future__ import annotations

import logging
from bisect import bisect_right
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from operator import attrgetter

from talaria.foundation.key_levels import KeyLevel, KeyLevelSet

logger = logging.getLogger(__name__)
MAX_VERSIONS_PER_SERIES = 256
_cutoff = attrgetter("cutoff_ts")


@dataclass(frozen=True, slots=True)
class LevelPublication:
    epoch: str
    arrival: tuple[int, int]
    version: KeyLevelSet | None


class KeyLevelRegistry:
    """按快照截止时刻选版。读写与图表投影均在服务事件循环内完成。"""

    def __init__(
        self, *, now_ms: Callable[[], int], on_latest: Callable[[KeyLevelSet], None] | None = None,
        on_register: Callable[[KeyLevelSet, int], None] | None = None,
    ) -> None:
        self._versions: dict[tuple[str, str], list[KeyLevelSet]] = {}
        self._now_ms = now_ms
        self._on_latest = on_latest
        self._on_register = on_register

    def register(self, version: KeyLevelSet) -> None:
        published = self._now_ms()
        if version.cutoff_ts > published:
            return
        if self._on_register is not None:
            self._on_register(version, published)
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
        if cutoff_ts < versions[0].cutoff_ts:
            logger.debug(
                "%s %s 的事件截止时刻 %s 早于最早关键位版本 %s",
                instrument_id, timeframe, cutoff_ts, versions[0].cutoff_ts,
            )
        version = self.version_at((instrument_id, timeframe), cutoff_ts)
        return [lv for lv in version.levels if lv.ts_available <= cutoff_ts] if version else []

    def current_levels(self, instrument: str, timeframe: str) -> list[KeyLevel]:
        return self.levels_at(instrument, timeframe, self._now_ms())

    def version_at(self, key: tuple[str, str], cutoff: int) -> KeyLevelSet | None:
        versions = self._versions.get(key, ())
        end = bisect_right(versions, cutoff, key=_cutoff)
        return versions[end - 1] if end else None

class FrozenLevelHistory:
    """按接收顺序重建当时的注册表，保留实盘的替换、容量和选版规则。"""

    def __init__(self, publications: Sequence[LevelPublication], snapshot_id: str, first_observation: int):
        self.known_ids = {level.level_id for row in publications if row.version for level in row.version.levels}
        # 图表快照自身的计算在冻结后发布，只用于识别引用。
        self._rows = iter(row for row in publications if row.version is None or row.version.snapshot_id != snapshot_id)
        self._next = next(self._rows, None)
        self._key: tuple[str, str] | None = None
        self._epoch: str | None = None
        self._first_observation = first_observation
        self._receipt = 0
        self._registry = KeyLevelRegistry(now_ms=lambda: self._receipt)
        self.covered = False
        self.missing = False

    def levels_at(self, close: int, receipt: int, order: int) -> Sequence[KeyLevel]:
        self._receipt = receipt
        while self._next and self._next.arrival <= (receipt, order):
            row = self._next
            if row.epoch != self._epoch:
                self._epoch = row.epoch
                self._registry = KeyLevelRegistry(now_ms=lambda: self._receipt)
                self._key = None
            if row.version is not None:
                self._registry.register(row.version)
                self._key = (row.version.instrument_id, row.version.timeframe)
            self._next = next(self._rows, None)
        selected = self._registry.version_at(self._key, close) if self._key is not None else None
        if close >= self._first_observation:
            if selected is None:
                self.missing = True
            else:
                self.covered = True
        return [lv for lv in selected.levels if lv.ts_available <= close] if selected else ()
