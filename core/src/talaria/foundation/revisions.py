"""按可获取时间截断再选修订（ADR-0018）。"""

from collections.abc import Callable, Hashable, Iterable
from typing import Protocol


class Revision(Protocol):
    @property
    def ts_event(self) -> int: ...

    @property
    def ts_available(self) -> int: ...

    @property
    def revision(self) -> int: ...


def latest_visible_revisions[Row: Revision](
    rows: Iterable[Row], *,
    as_of: int | None,
    identity: Callable[[Row], Hashable],
    ingested: Callable[[Row], int],
) -> list[Row]:
    """同一修订以可获取时间、摄取时间和输入顺序打破平局。"""
    chosen: dict[Hashable, tuple[tuple[int, int, int], Row]] = {}
    for row in rows:
        if as_of is not None and row.ts_available > as_of:
            continue
        key = identity(row)
        current = chosen.get(key)
        version = (row.revision, row.ts_available, ingested(row))
        if current is None or version >= current[0]:
            chosen[key] = (version, row)
    return sorted((row for _, row in chosen.values()), key=lambda row: row.ts_event)
