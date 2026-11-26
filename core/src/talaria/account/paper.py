"""账户进程的已确认草案入口。"""

from collections.abc import Callable, Sequence
from decimal import Decimal
from fractions import Fraction
import logging
from math import ceil, floor
from pathlib import Path
import sys

import httpx

from talaria import sdk
from talaria.account.contracts import (
    BarrierChangeRequest,
    BarrierReceipt,
    DecisionOrigin,
    EntryCandidate,
    ExecutionEvent,
    PlanReceipt,
    RejectedPlanRecord,
    RoundPhase,
    TradingState as AccountTradingState,
)
from talaria.foundation.bars import CompactBar
from talaria.foundation.paper import (
    ConfirmedDraft,
    DraftEvidence,
    DraftReceipt,
    PaperCommand,
    PaperCommandKind,
    PaperCommandReceipt,
)
from talaria.packs import DEFAULT_PACK, get_market_pack
from talaria.service_endpoint import read_service_connection

_MINUTE = 60_000
_LOG = logging.getLogger(__name__)


class DraftIntake:
    def __init__(
        self, freeze: Callable[[ConfirmedDraft, int], tuple[str, Sequence[CompactBar]] | None],
        submit: Callable[[EntryCandidate], PlanReceipt | RejectedPlanRecord],
        record: Callable[[ExecutionEvent], None], previous: Sequence[ExecutionEvent] = (),
        *, quantity_lookup: Callable[[str], Decimal | None] | None = None,
    ) -> None:
        self._freeze = freeze
        self._submit = submit
        self._record = record
        self._quantity_lookup = quantity_lookup
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
        approved_quantity: Decimal | None = None
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
                if approved_plan_id is not None and self._quantity_lookup is not None:
                    approved_quantity = self._quantity_lookup(approved_plan_id)
        receipt = DraftReceipt(
            draft_id=draft.draft_id, version=draft.version, accepted=reason is None,
            reason=reason, snapshot_id=snapshot_id, plan_id=approved_plan_id,
            quantity=approved_quantity,
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
    def __init__(
        self,
        data_dir: Path,
        accounts: Sequence[str],
        client: httpx.Client,
        *,
        on_service_warning: Callable[[str | None], None] | None = None,
    ) -> None:
        self._data_dir = data_dir
        self._accounts = accounts
        self._client = client
        self._on_service_warning = on_service_warning
        self._last_service_warning: str | None = None

    @property
    def service_warning(self) -> str | None:
        return self._last_service_warning

    def _set_service_warning(self, warning: str | None) -> None:
        if warning == self._last_service_warning:
            return
        self._last_service_warning = warning
        if warning is not None:
            _LOG.warning("PaperInbox %s", warning)
            print(f"[talaria-account WARNING] {warning}", file=sys.stderr, flush=True)
        else:
            _LOG.info("PaperInbox service connection recovered")
        if self._on_service_warning is not None:
            self._on_service_warning(warning)

    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        service = read_service_connection(self._data_dir)
        if service is None:
            raise httpx.TransportError("常驻服务离线")
        response = self._client.request(
            method, service.base_url + path, headers={"Authorization": f"Bearer {service.token}"}, **kwargs,
        )
        response.raise_for_status()
        self._set_service_warning(None)
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

    def claim_command(self, account: str) -> PaperCommand | None:
        if account not in self._accounts:
            raise ValueError("未知模拟盘子账户")
        try:
            response = self._request("POST", f"/api/paper/accounts/{account}/next-command")
            if response.json() is None:
                return None
            command = PaperCommand.model_validate_json(response.content)
            if command.sub_account_id != account:
                raise ValueError("常驻服务提交的干预命令与子账户不一致")
            return command
        except (httpx.TransportError, httpx.HTTPStatusError) as exc:
            self._retry_service_error(exc)
            return None

    def acknowledge_command(self, account: str, receipt: PaperCommandReceipt) -> None:
        try:
            self._request(
                "POST",
                f"/api/paper/accounts/{account}/command-receipt",
                json=receipt.model_dump(mode="json"),
            )
        except (httpx.TransportError, httpx.HTTPStatusError) as exc:
            self._retry_service_error(exc)

    def _retry_service_error(self, exc: httpx.HTTPError) -> None:
        if isinstance(exc, httpx.HTTPStatusError):
            status = exc.response.status_code
            if status < 500 and status not in (401, 403, 404, 429):
                raise exc
            if status in (401, 403, 404):
                code = "SERVICE_INCOMPATIBLE" if status == 404 else "SERVICE_AUTH_FAILED"
                method = exc.request.method if exc.request is not None else "HTTP"
                path = exc.request.url.path if exc.request is not None else ""
                self._set_service_warning(f"{code}: HTTP {status} on {method} {path}")


_TERMINAL_ROUND_PHASES = frozenset({RoundPhase.CLOSED, RoundPhase.CANCELLED, RoundPhase.LIQUIDATED})
_BARRIER_EVENT_KINDS = frozenset({"BARRIER_TIGHTENED", "BARRIER_AMENDED"})
_BARRIER_COMMAND_KINDS = frozenset({
    PaperCommandKind.TIGHTEN_STOP,
    PaperCommandKind.MOVE_TO_BREAKEVEN,
    PaperCommandKind.WIDEN_STOP,
    PaperCommandKind.MODIFY_TAKE_PROFIT,
})
_STATE_RANK = {
    AccountTradingState.ACTIVE: 0,
    AccountTradingState.REDUCING: 1,
    AccountTradingState.HALTED: 2,
}


def _snap_breakeven(entry: Decimal, direction: str, tick_size: Decimal) -> Decimal:
    ticks_frac = Fraction(entry) / Fraction(tick_size)
    ticks = ceil(ticks_frac) if direction == "LONG" else floor(ticks_frac)
    return Decimal(max(1, ticks)) * tick_size


class CommandIntake:
    """按 ADR-0015 底线 2 与 ADR-0025 D3 路由、幂等执行并记录人工干预命令。"""

    def __init__(
        self,
        gate,
        fill,
        record: Callable[[ExecutionEvent], None],
        previous: Sequence[ExecutionEvent] = (),
        *,
        ledger_events_reader: Callable[[], Sequence[ExecutionEvent]] | None = None,
        ledger_seq_reader: Callable[[], int] | None = None,
    ) -> None:
        self._gate = gate
        self._fill = fill
        self._record = record
        self._ledger_events_reader = ledger_events_reader
        self._ledger_seq_reader = ledger_seq_reader
        self._pack = get_market_pack(DEFAULT_PACK)
        self._receipts: dict[str, PaperCommandReceipt] = {}
        for event in previous:
            if event.kind == "COMMAND_RESOLVED" and event.reason:
                try:
                    parsed = PaperCommandReceipt.model_validate_json(event.reason)
                except ValueError:
                    continue
                self._receipts[parsed.command_id] = parsed

    def _journal_events(self) -> Sequence[ExecutionEvent]:
        if self._ledger_events_reader is not None:
            return self._ledger_events_reader()
        return ()

    def _is_barrier_stale(self, command: PaperCommand, current_seq: int) -> bool:
        if command.round_id is None or current_seq <= command.based_on_ledger_seq:
            return False
        events = self._journal_events()
        for event in events[command.based_on_ledger_seq :]:
            if (
                event.sub_account_id == command.sub_account_id
                and event.kind in _BARRIER_EVENT_KINDS
                and event.position is not None
                and event.position.round_id == command.round_id
            ):
                return True
        return False

    def _round_is_terminal(self, round_id: str) -> bool:
        pos = getattr(self._gate, "_positions", {}).get(round_id)
        if pos is not None and pos.quantity == 0 and pos.phase in _TERMINAL_ROUND_PHASES:
            return True
        if pos is not None and pos.quantity == 0 and pos.phase is None:
            return True
        for event in reversed(self._journal_events()):
            if event.position is not None and event.position.round_id == round_id:
                return event.position.quantity == 0 and (
                    event.position.phase in _TERMINAL_ROUND_PHASES
                    or event.kind in ("EXIT_FILLED", "ROUND_CLOSED", "ROUND_CANCELLED", "ROUND_LIQUIDATED")
                )
        return False

    def _flush_paper_reduction_now(self, round_id: str, account: str, now: int) -> None:
        """在模拟盘人工减仓/全平后立即按最新标记价或最近收盘价完成撮合，满足 <=2s 投影闭环。"""
        from talaria.account.plugins.bar_sim_1m import BarSim1m, _BarLiquidity

        if not isinstance(self._fill, BarSim1m):
            return
        executor = self._fill._rounds.get(round_id)
        if executor is None or executor.reduction is None or not executor.quantity:
            return
        instrument_id = executor.instrument.instrument_id
        state = self._gate.account_state(account)
        ref_price: Decimal | None = None
        mark = state.marks.get(instrument_id)
        if mark is not None and mark.value > 0:
            ref_price = mark.value
        else:
            last = self._fill._last_prices.get(instrument_id)
            if last is not None:
                ref_price = last[1]
        if ref_price is None:
            return
        prev_now = self._fill._now
        if now > prev_now:
            self._fill._now = now
        liq = _BarLiquidity(Fraction(0), executor.quantity)
        self._fill._fill_reduction(executor, liq, ref_price)
        self._gate.poll_events(now)

    def receive(self, command: PaperCommand, now: int, *, current_ledger_seq: int) -> PaperCommandReceipt:
        existing = self._receipts.get(command.command_id)
        if existing is not None:
            return existing

        accepted, reason = self._execute(command, now, current_ledger_seq)
        after_seq = (
            self._ledger_seq_reader()
            if self._ledger_seq_reader is not None
            else len(self._journal_events())
        )
        next_seq = max(current_ledger_seq, after_seq) + 1
        receipt = PaperCommandReceipt(
            command_id=command.command_id,
            sub_account_id=command.sub_account_id,
            accepted=accepted,
            reason=reason,
            ledger_seq=next_seq,
            processed_ts=now,
        )
        self._record(
            ExecutionEvent(
                kind="COMMAND_RESOLVED",
                sub_account_id=command.sub_account_id,
                ts_event=now,
                plan_id=command.command_id,
                reason=receipt.model_dump_json(),
            )
        )
        self._receipts[command.command_id] = receipt
        return receipt

    def _execute(
        self, command: PaperCommand, now: int, current_ledger_seq: int,
    ) -> tuple[bool, str | None]:
        account = command.sub_account_id
        kind = command.kind
        state = self._gate.account_state(account)

        if kind is PaperCommandKind.SET_TRADING_STATE:
            assert command.payload.trading_state is not None
            target = AccountTradingState(command.payload.trading_state.value)
            current = state.trading_state
            if target == current:
                return True, "NOOP"
            reason_text = command.payload.reason or "MANUAL_COMMAND"
            if _STATE_RANK[target] > _STATE_RANK[current]:
                # 收紧交易状态：直达执行层。
                self._gate.set_trading_state(account, target, reason_text)
                self._gate.record_host_event(
                    ExecutionEvent(
                        kind="TRADING_STATE_CHANGED",
                        sub_account_id=account,
                        ts_event=now,
                        reason=f"{reason_text}:{target.value}",
                    )
                )
                self._gate.poll_events(now)
                return True, None
            # 放宽交易状态（如恢复 ACTIVE）：先过风控检查。
            res = self._gate.relax_trading_state(account, target, reason_text)
            if isinstance(res, RejectedPlanRecord) or not res.accepted:
                return False, res.reason or "RISK_REJECTED"
            self._gate.poll_events(now)
            return True, res.reason

        assert command.round_id is not None
        round_id = command.round_id

        if self._round_is_terminal(round_id):
            return False, "COMMAND_STALE"

        if kind in _BARRIER_COMMAND_KINDS and self._is_barrier_stale(command, current_ledger_seq):
            return False, "COMMAND_STALE"

        position = state.rounds.get(round_id)
        has_reservation = any(item.round_id == round_id for item in state.reservations)

        if kind is PaperCommandKind.CANCEL_ENTRY:
            if not has_reservation:
                if position is not None:
                    return True, "NOOP"
                return False, "COMMAND_STALE"
            self._gate.intervene_position(
                round_id, "CANCEL_ENTRY", None, command.payload.reason or "MANUAL_CANCEL_ENTRY",
            )
            self._gate.poll_events(now)
            return True, None

        if kind is PaperCommandKind.CLOSE_ALL:
            if position is None and not has_reservation:
                return False, "COMMAND_STALE"
            self._gate.intervene_position(
                round_id, "CLOSE_ALL", None, command.payload.reason or "MANUAL_CLOSE_ALL",
            )
            self._flush_paper_reduction_now(round_id, account, now)
            self._gate.poll_events(now)
            return True, None

        if kind is PaperCommandKind.REDUCE_RATIO:
            if position is None:
                return False, "COMMAND_STALE"
            assert command.payload.ratio is not None
            before_qty = position.quantity
            self._gate.intervene_position(
                round_id,
                "REDUCE_RATIO",
                command.payload.ratio,
                command.payload.reason or "MANUAL_REDUCE_RATIO",
            )
            self._flush_paper_reduction_now(round_id, account, now)
            self._gate.poll_events(now)
            after_pos = self._gate.account_state(account).rounds.get(round_id)
            if after_pos is not None and after_pos.quantity == before_qty:
                return True, "NOOP"
            return True, None

        if position is None:
            return False, "COMMAND_STALE"

        if kind is PaperCommandKind.TIGHTEN_STOP:
            assert command.payload.stop is not None
            new_stop = command.payload.stop
            if new_stop == position.stop:
                return True, "NOOP"
            widens = new_stop < position.stop if position.direction == "LONG" else new_stop > position.stop
            if widens:
                return False, "STOP_WIDENING_REQUIRES_WIDEN_COMMAND"
            res = self._gate.change_barrier(
                BarrierChangeRequest(sub_account_id=account, round_id=round_id, stop=new_stop),
            )
            self._gate.poll_events(now)
            if isinstance(res, RejectedPlanRecord) or not res.accepted:
                return False, res.reason or "BARRIER_REJECTED"
            return True, None

        if kind is PaperCommandKind.MOVE_TO_BREAKEVEN:
            exposure = next((item for item in state.positions if item.round_id == round_id), None)
            if exposure is None:
                return False, "COMMAND_STALE"
            spec = self._pack.instrument(position.instrument_id)
            tick_size = spec.tick_size if spec is not None else Decimal("0.01")
            be_stop = _snap_breakeven(exposure.position.entry_price, position.direction, tick_size)
            widens = be_stop < position.stop if position.direction == "LONG" else be_stop > position.stop
            if be_stop == position.stop or widens:
                return True, "NOOP"
            res = self._gate.change_barrier(
                BarrierChangeRequest(sub_account_id=account, round_id=round_id, stop=be_stop),
            )
            self._gate.poll_events(now)
            if isinstance(res, RejectedPlanRecord) or not res.accepted:
                return False, res.reason or "BARRIER_REJECTED"
            return True, None

        if kind is PaperCommandKind.WIDEN_STOP:
            assert command.payload.stop is not None
            new_stop = command.payload.stop
            if new_stop == position.stop:
                return True, "NOOP"
            res = self._gate.change_barrier(
                BarrierChangeRequest(sub_account_id=account, round_id=round_id, stop=new_stop),
            )
            self._gate.poll_events(now)
            if isinstance(res, RejectedPlanRecord) or not res.accepted:
                return False, res.reason or "RISK_REJECTED"
            return True, None

        if kind is PaperCommandKind.MODIFY_TAKE_PROFIT:
            assert command.payload.take_profit is not None
            res = self._gate.change_take_profit(
                account,
                round_id,
                command.payload.take_profit,
                reason=command.payload.reason,
            )
            self._gate.poll_events(now)
            if isinstance(res, RejectedPlanRecord) or not res.accepted:
                return False, res.reason or "BARRIER_REJECTED"
            return True, res.reason

        return False, "UNSUPPORTED_COMMAND"
