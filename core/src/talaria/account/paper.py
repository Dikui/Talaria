"""账户进程的已确认草案入口。"""

from collections.abc import Callable, Sequence
from fractions import Fraction
from pathlib import Path

import httpx

from talaria import sdk
from talaria.account.contracts import DecisionOrigin, EntryCandidate, ExecutionEvent, PlanReceipt, RejectedPlanRecord
from talaria.foundation.bars import CompactBar
from talaria.foundation.paper import ConfirmedDraft, DraftEvidence, DraftReceipt
from talaria.service_endpoint import read_service_connection

_MINUTE = 60_000


class DraftIntake:
    def __init__(
        self, freeze: Callable[[ConfirmedDraft, int], tuple[str, Sequence[CompactBar]] | None],
        submit: Callable[[EntryCandidate], PlanReceipt | RejectedPlanRecord],
        record: Callable[[ExecutionEvent], None], previous: Sequence[ExecutionEvent] = (),
    ) -> None:
        self._freeze = freeze
        self._submit = submit
        self._record = record
        self._receipts = {
            (event.draft_receipt.draft_id, event.draft_receipt.version): event.draft_receipt
            for event in previous if event.kind == "DRAFT_RESOLVED" and event.draft_receipt is not None
            and event.draft_receipt.reason not in ("DRAFT_DATA_UNAVAILABLE", "DRAFT_INTERRUPTED")
        }

        # 旧宿主在同一次串行提交中先写临时接收标记，紧接着写成交或拒单事实。
        for marker, result in zip(previous, previous[1:]):
            interim = marker.draft_receipt
            if marker.kind != "DRAFT_RECEIVED" or interim is None or result.ts_event != marker.ts_event:
                continue
            if result.kind in ("PLAN_SUBMITTED", "CANDIDATE_REJECTED"):
                self._receipts.setdefault((interim.draft_id, interim.version), interim.model_copy(update={
                    "accepted": result.kind == "PLAN_SUBMITTED", "reason": result.reason, "plan_id": result.plan_id,
                }))
        self._pending = {(event.draft_id, event.draft_version) for event in previous if event.kind == "DRAFT_PENDING"}

    def stage(self, draft: ConfirmedDraft, now: int) -> None:
        key = draft.draft_id, draft.version
        if key not in self._pending and key not in self._receipts:
            self._record(ExecutionEvent(
                kind="DRAFT_PENDING", sub_account_id=draft.content.sub_account_id,
                draft_id=draft.draft_id, draft_version=draft.version, ts_event=now,
            ))
            self._pending.add(key)

    def receive(self, draft: ConfirmedDraft, now: int) -> DraftReceipt | None:
        key = draft.draft_id, draft.version
        if key in self._receipts:
            return self._receipts[key]
        self.stage(draft, now)
        content = draft.content
        plan_id = f"draft-{draft.draft_id}-v{draft.version}"
        frozen = self._freeze(draft, now)
        if frozen is None:
            return None
        snapshot_id, bars = frozen
        reason = _stale_reason(draft, bars, now)

        approved_plan_id = None
        if reason == "DRAFT_DATA_UNAVAILABLE":
            return None
        if reason is None:
            result = self._submit(EntryCandidate(
                sub_account_id=content.sub_account_id, instrument_id=content.instrument_id,
                direction=content.direction, entry_low=content.entry_low, entry_high=content.entry_high,
                invalidation_stop=content.invalidation_stop, take_profit=content.take_profit,
                ts_decision=now, origin=DecisionOrigin.CO_PILOT if content.consultation_id else DecisionOrigin.HUMAN,
                round_id=content.round_id, inherited_barriers=content.inherited_barriers, draft_plan_id=plan_id,
                valid_until=content.valid_until, evidence_ids=(content.snapshot_id,),
                snapshot_id=snapshot_id, deliberation_ms=draft.deliberation_ms,
            ))
            if isinstance(result, RejectedPlanRecord) or not result.accepted:
                reason = result.reason or "FILL_REJECTED"
            else:
                approved_plan_id = result.plan_id
        receipt = DraftReceipt(
            draft_id=draft.draft_id, version=draft.version, accepted=reason is None,
            reason=reason, snapshot_id=snapshot_id, plan_id=approved_plan_id,
        )
        self._record(ExecutionEvent(
            kind="DRAFT_RESOLVED", sub_account_id=content.sub_account_id, ts_event=now, reason=reason,
            draft_id=draft.draft_id, draft_version=draft.version, plan_id=approved_plan_id,
            draft_receipt=receipt, deliberation_ms=draft.deliberation_ms,
        ))
        self._receipts[key] = receipt
        return receipt


def _stale_reason(draft: ConfirmedDraft, bars: Sequence[CompactBar], now: int) -> str | None:
    content = draft.content
    if content.valid_until is not None and now > content.valid_until:
        return "DRAFT_STALE"
    start = max(draft.created_ts, draft.basis_cutoff_ts)
    first = start // _MINUTE * _MINUTE
    last = (now - 1) // _MINUTE * _MINUTE if now > start else first
    covered = set()
    for bar in bars:
        if not first <= bar.timestamp <= now or bar.close is None or bar.ts_available > now:
            continue
        if bar.timestamp % _MINUTE:
            continue
        if bar.timestamp <= last:
            covered.add(bar.timestamp)
        if _crossed(draft, bar):
            return "DRAFT_STALE"
    if len(covered) != (last - first) // _MINUTE + 1:
        return "DRAFT_DATA_UNAVAILABLE"
    return None


def _crossed(draft: ConfirmedDraft, bar: CompactBar) -> bool:
    content = draft.content
    if content.direction == "LONG":
        return bar.low <= content.invalidation_stop or bar.high > content.entry_high
    return bar.high >= content.invalidation_stop or bar.low < content.entry_low


class PaperInbox:
    def __init__(self, data_dir: Path, accounts: Sequence[str], client: httpx.Client) -> None:
        self._data_dir = data_dir
        self._accounts = accounts
        self._client = client

    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        service = read_service_connection(self._data_dir)
        if service is None:
            raise httpx.TransportError("常驻服务离线")
        response = self._client.request(
            method, service.base_url + path, headers={"Authorization": f"Bearer {service.token}"}, **kwargs,
        )
        response.raise_for_status()
        return response

    def freeze(self, draft: ConfirmedDraft, cutoff: int) -> tuple[str, Sequence[CompactBar]] | None:
        try:
            return self._evidence(draft, cutoff)
        except (httpx.TransportError, httpx.HTTPStatusError) as exc:
            self._retry_service_error(exc)
            return None

    def _evidence(self, draft: ConfirmedDraft, cutoff: int) -> tuple[str, Sequence[CompactBar]]:
        instrument = draft.content.instrument_id
        snapshot = DraftEvidence.model_validate_json(self._request("POST", "/api/paper/draft-evidence", json={
            "instrument_id": instrument, "timeframes": ["1m"], "cutoff_ts": cutoff,
        }).content)
        if (snapshot.instrument_id, snapshot.cutoff_ts) != (instrument, cutoff):
            raise ValueError("草案重新冻结的证据快照与请求不一致")
        start = max(draft.created_ts, draft.basis_cutoff_ts)
        first = start // _MINUTE * _MINUTE
        bars = snapshot.bars
        while bars and bars[0].timestamp > first:
            page = self._request("GET", f"/api/snapshots/{snapshot.snapshot_id}/bars", params={
                "timeframe": "1m", "limit": 1000, "before_ts": bars[0].timestamp,
            }).json()
            earlier = [CompactBar.model_validate(row) for row in page["bars"]]
            if not earlier:
                break
            if earlier[-1].timestamp >= bars[0].timestamp:
                raise ValueError("冻结快照分页游标未前进")
            bars = earlier + bars
        if snapshot.current_bar is not None:
            bars = [*bars, snapshot.current_bar]
        evidence = []
        for bar in bars:
            if bar.timestamp < first:
                continue
            if bar.timestamp == first and start != first and bar.close is not None and _crossed(draft, bar):
                # 累计高低价越界时，以成交量核对首分钟逐笔是否齐全，再截取实际审阅区间。
                ticks = sdk.trades(instrument, first, min(first + _MINUTE - 1, cutoff), as_of=cutoff,
                                   data_dir=self._data_dir)
                prices = [t.price for t in ticks if t.ts_event >= start]
                if prices:
                    clipped = bar.model_copy(update={"low": min(prices), "high": max(prices)})
                    if _crossed(draft, clipped):
                        evidence.append(clipped)
                        continue
                volume = sum((Fraction(t.size) for t in ticks), Fraction(0))
                if volume != Fraction(bar.volume) or (ticks and
                    (min(t.price for t in ticks), max(t.price for t in ticks)) != (bar.low, bar.high)
                ):
                    continue
                if not prices:
                    prices = [bar.close]
                bar = bar.model_copy(update={"low": min(prices), "high": max(prices)})
            evidence.append(bar)
        return snapshot.snapshot_id, evidence

    def claim(self, account: str, instrument: str, cutoff: int) -> ConfirmedDraft | None:
        if account not in self._accounts:
            raise ValueError("未知模拟盘子账户")
        endpoint = f"/api/paper/accounts/{account}"
        try:
            response = self._request("POST", endpoint + "/next-draft", json={"instrument_id": instrument})
            if response.json() is None:
                return None
            draft = ConfirmedDraft.model_validate_json(response.content)
            if (draft.content.sub_account_id, draft.content.instrument_id) != (account, instrument):
                raise ValueError("常驻服务提交的草案与账户或实时标的不一致")
            if draft.confirmed_ts > cutoff:
                return None
            return draft
        except (httpx.TransportError, httpx.HTTPStatusError) as exc:
            self._retry_service_error(exc)
            return None

    def acknowledge(self, account: str, receipt: DraftReceipt) -> None:
        try:
            self._request("POST", f"/api/paper/accounts/{account}/draft-receipt", json=receipt.model_dump(mode="json"))
        except (httpx.TransportError, httpx.HTTPStatusError) as exc:
            self._retry_service_error(exc)

    @staticmethod
    def _retry_service_error(exc: httpx.HTTPError) -> None:
        if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code < 500 and exc.response.status_code not in (401, 403, 429):
            raise exc
