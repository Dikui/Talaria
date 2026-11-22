"""上游实时订阅配额；多个使用方共享同一标的与周期的通道。"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from talaria.feed.supervisor import FeedSupervisor
from talaria.foundation.errors import TalariaError
from talaria.foundation.watchlist import DegradationReason

WATCHLIST_TIMEFRAME = "1m"

type SubscriptionKey = tuple[str, str]
type Owner = tuple[str, str, SubscriptionKey]


class SubscriptionQuotaError(TalariaError):
    code = "SUBSCRIPTION_QUOTA"


@dataclass(frozen=True)
class SubscriptionView:
    selected: frozenset[str]
    subscribed: frozenset[SubscriptionKey]
    degraded: dict[str, DegradationReason]
    chart_instruments: frozenset[str]


class RealtimeSubscriptionScheduler:
    def __init__(
        self,
        supervisor: FeedSupervisor,
        *,
        quota: int,
        watchlist_timeframe: str = WATCHLIST_TIMEFRAME,
        on_alert_quota: Callable[[list[str]], None] | None = None,
        on_degraded: Callable[[Owner], None] | None = None,
    ) -> None:
        if quota <= 0:
            raise ValueError("实时订阅配额必须为正数")
        self._supervisor = supervisor
        self._quota = quota
        self._watchlist_timeframe = watchlist_timeframe
        self._on_alert_quota = on_alert_quota
        self._on_degraded = on_degraded
        self._accounts: dict[str, SubscriptionKey] = {}
        self._chart_refs: Counter[SubscriptionKey] = Counter()
        self._managed: set[SubscriptionKey] = set()
        self._watchlist_order: tuple[str, ...] = ()
        self._alerts: dict[str, SubscriptionKey] = {}
        self._denied: set[Owner] = set()

    @property
    def quota(self) -> int:
        return self._quota

    @property
    def used(self) -> int:
        return len(self._managed)

    @property
    def denied_alerts(self) -> list[str]:
        return sorted(owner_id for kind, owner_id, _ in self._denied if kind == "alert")

    def check_account(self, owner: str, key: SubscriptionKey) -> None:
        if self._accounts.get(owner) == key:
            return
        if owner in self._accounts:
            raise ValueError("同一账户订阅标识只能持有一个通道")
        # 已用配额保留给现有使用方。订阅同一通道可以共享配额。
        if key not in self._managed and self.used >= self.quota:
            raise self._quota_error(key)

    def acquire_account(self, owner: str, key: SubscriptionKey) -> None:
        self.check_account(owner, key)
        self._accounts[owner] = key
        self._assign()

    def release_account(self, owner: str) -> None:
        if self._accounts.pop(owner, None) is not None:
            self._assign()

    def acquire_chart(self, key: SubscriptionKey) -> None:
        protected = set(self._accounts.values()).union(self._chart_refs)
        if key not in protected and len(protected) >= self.quota:
            raise self._quota_error(key)
        self._chart_refs[key] += 1
        self._assign()

    def release_chart(self, key: SubscriptionKey) -> None:
        remaining = self._chart_refs[key]
        if remaining == 1:
            del self._chart_refs[key]
            self._assign()
        elif remaining > 1:
            self._chart_refs[key] = remaining - 1

    def reconcile(
        self,
        instrument_ids: Sequence[str],
        *,
        alerts: Mapping[str, SubscriptionKey] | None = None,
    ) -> SubscriptionView:
        self._watchlist_order = tuple(dict.fromkeys(instrument_ids))
        self._alerts = dict(alerts or {})
        self._assign()
        selected = frozenset(
            instrument for instrument, timeframe in self._managed
            if timeframe == self._watchlist_timeframe
        )
        return SubscriptionView(
            selected=selected,
            subscribed=frozenset(self._managed),
            degraded={
                instrument: DegradationReason.QUOTA
                for instrument in self._watchlist_order
                if instrument not in selected
            },
            chart_instruments=frozenset(instrument for instrument, _ in self._chart_refs),
        )

    def _assign(self) -> None:
        wanted = set(self._accounts.values()).union(self._chart_refs)
        denied: set[Owner] = set()
        owners = [
            ("alert", alert_id, key) for alert_id, key in self._alerts.items()
        ] + [
            ("watchlist", instrument, (instrument, self._watchlist_timeframe))
            for instrument in self._watchlist_order
        ]
        for owner in owners:
            key = owner[2]
            if key in wanted or len(wanted) < self._quota:
                wanted.add(key)
            else:
                denied.add(owner)

        for instrument, timeframe in sorted(self._managed - wanted):
            self._supervisor.unsubscribe(instrument_id=instrument, timeframe=timeframe)
            self._managed.remove((instrument, timeframe))
        for instrument, timeframe in sorted(wanted - self._managed):
            self._supervisor.subscribe(instrument_id=instrument, timeframe=timeframe)
            self._managed.add((instrument, timeframe))

        newly_denied = denied - self._denied
        previous_alerts = self.denied_alerts
        self._denied = denied
        if self._on_degraded is not None:
            for owner in sorted(newly_denied):
                self._on_degraded(owner)
        if self._on_alert_quota is not None and previous_alerts != self.denied_alerts:
            self._on_alert_quota(self.denied_alerts)

    @staticmethod
    def _quota_error(key: SubscriptionKey) -> SubscriptionQuotaError:
        return SubscriptionQuotaError(f"实时订阅配额已满：{key[0]} {key[1]}")
