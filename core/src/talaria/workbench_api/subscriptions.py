"""实时订阅配额调度：当前图表永远优先，自选按顺序占用剩余配额。

调度器是 ``FeedSupervisor`` 之前的唯一订阅协调点：WebSocket 图表订阅通过
``acquire_chart`` / ``release_chart`` 登记引用，Watchlist 每次读取时调用
``reconcile`` 重新分配。这样同一个 ``(标的, 周期)`` 只会订阅一次，图表与
Watchlist 共用时也不会互相取消。任务单 13 通过 ``alerted`` 参数接入
“挂有活跃预警”优先级；配额分配顺序固定为：当前图表 > 活跃预警 > 普通自选。
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

from talaria.feed.supervisor import FeedSupervisor
from talaria.foundation.watchlist import DegradationReason

WATCHLIST_TIMEFRAME = "1m"


@dataclass(frozen=True)
class SubscriptionView:
    """一次配额分配的结果，供 Watchlist 渲染与测试断言。"""

    selected: frozenset[str]
    subscribed: frozenset[tuple[str, str]]
    degraded: dict[str, DegradationReason]
    chart_instruments: frozenset[str]


class RealtimeSubscriptionScheduler:
    """管理 supervisor 的实际订阅，保证图表优先且不重复订阅。"""

    def __init__(
        self,
        supervisor: FeedSupervisor,
        *,
        quota: int,
        watchlist_timeframe: str = WATCHLIST_TIMEFRAME,
    ) -> None:
        if quota <= 0:
            raise ValueError("实时订阅配额必须为正数")
        self._supervisor = supervisor
        self._quota = quota
        self._watchlist_timeframe = watchlist_timeframe
        self._chart_refs: Counter[tuple[str, str]] = Counter()
        self._managed: set[tuple[str, str]] = set()
        self._watchlist_order: tuple[str, ...] = ()

    @property
    def quota(self) -> int:
        return self._quota

    @property
    def used(self) -> int:
        return len(self._managed)

    def acquire_chart(self, key: tuple[str, str]) -> None:
        """登记一个图表订阅；``FeedSupervisor`` 只在首次需要时收到 subscribe。"""
        if self._chart_refs[key] == 0 and key not in self._managed:
            self._supervisor.subscribe(instrument_id=key[0], timeframe=key[1])
            self._managed.add(key)
        self._chart_refs[key] += 1

    def release_chart(self, key: tuple[str, str]) -> None:
        """释放图表引用；实际退订留给下一次 ``reconcile``，避免与自选争抢。"""
        remaining = self._chart_refs[key]
        if remaining <= 1:
            self._chart_refs.pop(key, None)
        else:
            self._chart_refs[key] = remaining - 1

    def reconcile(
        self,
        instrument_ids: Sequence[str],
        *,
        alerted: Sequence[str] = (),
    ) -> SubscriptionView:
        """按“图表 > 活跃预警 > 自选顺序”分配配额并同步订阅。"""
        self._watchlist_order = tuple(dict.fromkeys(instrument_ids))
        chart_pairs = set(self._chart_refs)
        selected: set[str] = {instrument for instrument, _ in chart_pairs}
        wanted = set(chart_pairs)
        priority_order = tuple(dict.fromkeys((*alerted, *self._watchlist_order)))

        for instrument_id in priority_order:
            if instrument_id in selected:
                continue
            if len(wanted) >= self._quota:
                continue
            pair = (instrument_id, self._watchlist_timeframe)
            wanted.add(pair)
            selected.add(instrument_id)

        degraded = {
            instrument_id: DegradationReason.QUOTA
            for instrument_id in self._watchlist_order
            if instrument_id not in selected
        }

        # 先退订不再需要的，再补订阅，避免瞬时超过配额。
        for pair in sorted(self._managed - wanted):
            self._supervisor.unsubscribe(instrument_id=pair[0], timeframe=pair[1])
            self._managed.discard(pair)
        for pair in sorted(wanted - self._managed):
            self._supervisor.subscribe(instrument_id=pair[0], timeframe=pair[1])
            self._managed.add(pair)

        return SubscriptionView(
            selected=frozenset(selected),
            subscribed=frozenset(self._managed),
            degraded=degraded,
            chart_instruments=frozenset(
                instrument for instrument, _ in self._chart_refs
            ),
        )
