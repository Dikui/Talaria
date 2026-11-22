"""按开盘槽保留确认修订，为实时规则重建某一时点的观察。"""

from bisect import bisect_left
from collections.abc import Iterator

from talaria.foundation.bars import CompactBar


class ConfirmedHistory:
    def __init__(self, interval: int, limit: int):
        self.interval = interval
        self.limit = limit
        self._slots: list[int] = []
        self._versions: dict[int, dict[tuple[int, int], CompactBar]] = {}

    def __contains__(self, timestamp: int) -> bool:
        return timestamp in self._versions

    def put(self, bar: CompactBar, revision: int = 0, *, visible_at: int | None = None) -> None:
        if visible_at is not None and visible_at > bar.ts_available:
            bar = bar.model_copy(update={"ts_available": visible_at})
        ts = bar.timestamp
        if ts not in self._versions:
            self._slots.insert(bisect_left(self._slots, ts), ts)
            self._versions[ts] = {}
            if len(self._slots) > self.limit:
                del self._versions[self._slots.pop(0)]
                if ts not in self._versions:
                    return
        self._versions[ts][revision, bar.ts_available] = bar

    def latest_slot(self, as_of: int) -> int | None:
        return next((ts for ts in reversed(self._slots)
                     if ts + self.interval <= as_of and any(k[1] <= as_of for k in self._versions[ts])), None)

    def visible(self, as_of: int, *, through: int | None = None) -> list[CompactBar]:
        end = as_of - self.interval
        if through is not None:
            end = min(end, through)
        visible = []
        for ts in self._slots:
            if ts > end:
                break
            versions = self._versions[ts]
            key = max((k for k in versions if k[1] <= as_of), default=None)
            if key is not None:
                visible.append(versions[key])
        return visible

    def observations_before(self, through: int, as_of: int) -> Iterator[tuple[int, int]]:
        """按可见时间重放至本次观察之前。旧槽的修订会重算最新可见槽。"""
        updates = []
        for ts in self._slots:
            if ts > through:
                break
            for revision, available in self._versions[ts]:
                cutoff = max(available, ts + self.interval)
                if cutoff <= as_of:
                    updates.append((cutoff, ts, revision))
        latest = -1
        last = None
        for cutoff, ts, revision in sorted(updates):
            if ts < latest and revision == 0:
                continue
            latest = max(latest, ts)
            observation = (cutoff, latest)
            if observation != last and observation != (as_of, through):
                yield observation
            last = observation
