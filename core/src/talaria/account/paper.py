"""账户进程的已确认草案入口。"""

from collections.abc import Callable, Sequence
from pathlib import Path

import httpx

from talaria.account.contracts import DecisionOrigin, EntryCandidate, ExecutionEvent, PlanReceipt, RejectedPlanRecord
from talaria.foundation.bars import CompactBar
from talaria.foundation.paper import ConfirmedDraft, DraftEvidence, DraftReceipt
from talaria.service_endpoint import read_service_connection

_MINUTE = 60_000


class DraftIntake:
    def __init__(
        self, freeze: Callable[[ConfirmedDraft, int], tuple[str, Sequence[CompactBar]]],
        submit: Callable[[EntryCandidate], PlanReceipt | RejectedPlanRecord],
        record: Callable[[ExecutionEvent], None], previous: Sequence[ExecutionEvent] = (),
    ) -> None:
        self._freeze = freeze
        self._submit = submit
        self._record = record
        self._receipts = {
            (event.draft_receipt.draft_id, event.draft_receipt.version): event.draft_receipt
            for event in previous if event.draft_receipt is not None
        }

    def receive(self, draft: ConfirmedDraft, now: int) -> DraftReceipt:
        key = draft.draft_id, draft.version
        if key in self._receipts:
            return self._receipts[key]
        content = draft.content
        snapshot_id, bars = self._freeze(draft, now)
        reason = _stale_reason(draft, bars, now)

        def write(kind: str, reason: str | None) -> DraftReceipt:
            receipt = DraftReceipt(
                draft_id=draft.draft_id, version=draft.version, accepted=reason is None,
                reason=reason, snapshot_id=snapshot_id,
            )
            self._record(ExecutionEvent(
                kind=kind, sub_account_id=content.sub_account_id, ts_event=now, reason=reason,
                draft_receipt=receipt, deliberation_ms=draft.deliberation_ms,
            ))
            self._receipts[key] = receipt
            return receipt

        if reason is None:
            # 先持久化接收标记。若进程中途中止，重复投递也不能再次进入成交。
            write("DRAFT_RECEIVED", "DRAFT_INTERRUPTED")
            result = self._submit(EntryCandidate(
                sub_account_id=content.sub_account_id, instrument_id=content.instrument_id,
                direction=content.direction, entry_low=content.entry_low, entry_high=content.entry_high,
                invalidation_stop=content.invalidation_stop, take_profit=content.take_profit,
                ts_decision=now, origin=DecisionOrigin.CO_PILOT if content.consultation_id else DecisionOrigin.HUMAN,
                valid_until=content.valid_until, evidence_ids=(content.snapshot_id,),
                snapshot_id=snapshot_id, deliberation_ms=draft.deliberation_ms,
            ))
            if isinstance(result, RejectedPlanRecord) or not result.accepted:
                reason = result.reason or "FILL_REJECTED"
        return write("DRAFT_RESOLVED", reason)


def _stale_reason(draft: ConfirmedDraft, bars: Sequence[CompactBar], now: int) -> str | None:
    content = draft.content
    if content.valid_until is not None and now > content.valid_until:
        return "DRAFT_STALE"
    first = draft.basis_cutoff_ts // _MINUTE * _MINUTE
    last = now // _MINUTE * _MINUTE - _MINUTE
    latest = next((bar for bar in reversed(bars) if bar.timestamp == last), None)
    span = [bar for bar in bars if first <= bar.timestamp <= last and bar.close is not None]
    observed = [bar for bar in bars if first <= bar.timestamp <= now and bar.close is not None]
    if content.direction == "LONG":
        crossed = any(bar.low <= content.invalidation_stop or bar.high > content.entry_high for bar in observed)
        outside = latest and latest.close is not None and not content.invalidation_stop < latest.close <= content.entry_high
    else:
        crossed = any(bar.high >= content.invalidation_stop or bar.low < content.entry_low for bar in observed)
        outside = latest and latest.close is not None and not content.entry_low <= latest.close < content.invalidation_stop
    if crossed or outside:
        return "DRAFT_STALE"
    if latest is None or latest.close is None or len(span) != max(0, (last - first) // _MINUTE + 1):
        return "DRAFT_DATA_UNAVAILABLE"
    return None


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

    def freeze(self, draft: ConfirmedDraft, cutoff: int) -> tuple[str, Sequence[CompactBar]]:
        instrument = draft.content.instrument_id
        snapshot = DraftEvidence.model_validate_json(self._request("POST", "/api/paper/draft-evidence", json={
            "instrument_id": instrument, "timeframes": ["1m"], "cutoff_ts": cutoff,
        }).content)
        if (snapshot.instrument_id, snapshot.cutoff_ts) != (instrument, cutoff):
            raise ValueError("草案重新冻结的证据快照与请求不一致")
        bars = snapshot.bars
        if snapshot.current_bar is not None:
            bars = [*bars, snapshot.current_bar]
        return snapshot.snapshot_id, bars

    def poll(self, instrument: str, cutoff: int, intake: DraftIntake) -> None:
        for account in self._accounts:
            endpoint = f"/api/paper/accounts/{account}"
            try:
                response = self._request("POST", endpoint + "/next-draft", json={"instrument_id": instrument})
                if response.json() is None:
                    continue
                draft = ConfirmedDraft.model_validate_json(response.content)
                if (draft.content.sub_account_id, draft.content.instrument_id) != (account, instrument):
                    raise ValueError("常驻服务提交的草案与账户或实时标的不一致")
                if draft.confirmed_ts > cutoff:
                    continue
                receipt = intake.receive(draft, cutoff)
                self._request("POST", endpoint + "/draft-receipt", json=receipt.model_dump(mode="json"))
            except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                if (
                    isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code < 500
                    and exc.response.status_code not in (401, 403)
                ):
                    raise
                # 保留本地回执和服务侧投递状态，继续消费行情并在下次脉冲重试。
                continue
