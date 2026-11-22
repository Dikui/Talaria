"""模拟盘草案与账本查询接口。"""

from fastapi import APIRouter, Request
from starlette.concurrency import run_in_threadpool

from talaria.account.paper_ledger import account_view, list_accounts
from talaria.foundation.bars import CompactBar, classify_flat_bar
from talaria.foundation.errors import InvalidRequestError, NotFoundError
from talaria.foundation.paper import (
    AccountId, ConfirmedDraft, DraftConfirmation, DraftContent, DraftDeliveryRequest, DraftEdit, DraftEvidence, DraftReceipt,
    DraftRecord, PaperAccountView, PaperRound,
)
from talaria.foundation.snapshot import SnapshotRequest
from talaria.perception.bar_range import range_visible_bars
from talaria.workbench_api.routes import get_runtime

router = APIRouter(prefix="/api/paper")


@router.get("/accounts")
def accounts(request: Request) -> list[AccountId]:
    return list_accounts(request.app.state.settings.data_dir)


@router.get("/accounts/{account}")
def ledger(account: AccountId, request: Request) -> PaperAccountView:
    view = account_view(request.app.state.settings.data_dir, account)
    if view is None:
        raise NotFoundError("找不到模拟盘账本")
    return view


@router.get("/barriers")
def barriers(instrument: str, request: Request) -> list[PaperRound]:
    root = request.app.state.settings.data_dir
    positions = []
    for account in list_accounts(root):
        view = account_view(root, account)
        if view is not None:
            positions.extend(row for row in view.positions if row.instrument_id == instrument)
    return positions


async def _basis(request: Request, content: DraftContent) -> int:
    runtime = get_runtime(request)
    snapshot = await run_in_threadpool(runtime.workbench.get_snapshot, content.snapshot_id)
    if snapshot.instrument_id != content.instrument_id or snapshot.cutoff_ts > request.app.state.settings.now_ms():
        raise InvalidRequestError("草案与证据快照的标的或时刻不符")
    if content.consultation_id is not None:
        record = await runtime.state_writer.read(lambda store: store.consultations.get(content.consultation_id))
        if record.insight is None or record.snapshot_id != content.snapshot_id:
            raise InvalidRequestError("草案与会诊的证据快照不符")
    return snapshot.cutoff_ts


@router.post("/draft-evidence")
async def freeze_draft(payload: SnapshotRequest, request: Request) -> DraftEvidence:
    if payload.timeframes != ["1m"] or payload.cutoff_ts is None:
        raise InvalidRequestError("草案重冻结需要明确的截止时刻与 1m 周期")
    runtime = get_runtime(request)
    cutoff = payload.cutoff_ts
    opening = cutoff // 60_000 * 60_000
    current = None
    if runtime.hot_buffer is not None:
        versions = runtime.hot_buffer.revisions(
            instrument_id=payload.instrument_id, timeframe="1m", start_ts=opening, end_ts=opening,
        )
        visible = range_visible_bars(
            (), [row for row in versions if row.ts_committed <= cutoff], as_of=cutoff, include_open=True,
        )
        raw = next((row for row in visible if not row.confirmed and not row.synthetic), None)
        if raw is not None:
            current = CompactBar(
                timestamp=opening, open=raw.open, high=raw.high, low=raw.low, close=raw.close,
                volume=raw.volume, turnover=raw.turnover, ts_available=raw.ts_available,
                status=classify_flat_bar(open=raw.open, high=raw.high, low=raw.low, close=raw.close, volume=raw.volume),
                precision=raw.precision,
            )
    snapshot = await runtime.workbench.create_snapshot_async(payload)
    page = await run_in_threadpool(
        runtime.workbench.get_bars, snapshot.snapshot_id, timeframe="1m", limit=1000, before_ts=None,
    )
    return DraftEvidence(
        snapshot_id=snapshot.snapshot_id, instrument_id=snapshot.instrument_id,
        cutoff_ts=snapshot.cutoff_ts, bars=page.bars, current_bar=current,
    )


@router.get("/drafts")
async def drafts(request: Request) -> list[DraftRecord]:
    return await get_runtime(request).state_writer.read(lambda store: store.paper.list())


@router.post("/drafts")
async def save(content: DraftContent, request: Request) -> DraftRecord:
    basis = await _basis(request, content)
    return await get_runtime(request).state_writer.run(lambda store: store.paper.save(content, basis))


@router.put("/drafts/{draft_id}")
async def edit(draft_id: str, payload: DraftEdit, request: Request) -> DraftRecord:
    basis = await _basis(request, payload.content)
    return await get_runtime(request).state_writer.run(
        lambda store: store.paper.edit(draft_id, payload.version, payload.content, basis),
    )


@router.post("/drafts/{draft_id}/confirm")
async def confirm(draft_id: str, payload: DraftConfirmation, request: Request) -> DraftRecord:
    return await get_runtime(request).state_writer.run(lambda store: store.paper.confirm(draft_id, payload))


@router.post("/accounts/{account}/next-draft")
async def next_draft(account: AccountId, payload: DraftDeliveryRequest, request: Request) -> ConfirmedDraft | None:
    return await get_runtime(request).state_writer.run(
        lambda store: store.paper.claim(account, payload.instrument_id),
    )


@router.post("/accounts/{account}/draft-receipt")
async def acknowledge(account: AccountId, payload: DraftReceipt, request: Request) -> DraftReceipt:
    return await get_runtime(request).state_writer.run(lambda store: store.paper.acknowledge(account, payload))
