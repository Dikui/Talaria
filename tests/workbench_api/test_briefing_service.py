"""协同简报服务：冷却、令牌桶、降级、快照绑定与 Token 幂等。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from decimal import Decimal
from typing import Any

import pytest

from talaria.foundation.briefings import (
    BriefingContent,
    BriefingLevelRef,
    BriefingStatus,
    BriefingResultRequest,
    ScannerBriefing,
)
from talaria.foundation.collaboration import UsageReport
from talaria.foundation.scanner import (
    ScannerConditionKind,
    ScannerEvidence,
    ScannerSeverity,
    ScannerTrigger,
    ScannerTriggerRoute,
)
from talaria.perception.scanner import TriggerRoutingPolicy
from talaria.workbench_api.briefing_service import BriefingService
from talaria.workbench_api.state_store import StateWriterQueue
from talaria.workbench_api.usage_service import UsageService

pytestmark = pytest.mark.medium

NOW_MS = 1_795_022_974_103
INSTRUMENT = "BTC-USDT-PERP"


class Clock:
    def __init__(self, now: int = NOW_MS) -> None:
        self.now = now

    def __call__(self) -> int:
        return self.now


class FakeLevel:
    def __init__(self) -> None:
        self.level_id = "lvl_1"
        self.price = Decimal("101.5")
        self.price_low = None
        self.price_high = None


class FakeKeyLevelSet:
    def __init__(self) -> None:
        self.levels = [FakeLevel()]


class FakeWorkbench:
    def __init__(self) -> None:
        self.calls = 0

    def get_key_levels(self, snapshot_id: str, *, timeframe: str, top_n: int):
        self.calls += 1
        return FakeKeyLevelSet()


class Host:
    def __init__(self, url: str | None = "http://127.0.0.1:65530") -> None:
        self.url = url
        self.calls: list[dict[str, Any]] = []

    def __call__(self) -> str | None:
        return self.url

    async def dispatch(self, url: str, payload: Mapping[str, Any]) -> None:
        self.calls.append({"url": url, **dict(payload)})


def _trigger(
    *,
    instrument: str = INSTRUMENT,
    snapshot: str = "snap_1",
    trigger_id: str = "scn_1",
) -> ScannerTrigger:
    return ScannerTrigger(
        trigger_id=trigger_id,
        instrument_id=instrument,
        timeframe="1m",
        kind=ScannerConditionKind.LIQUIDITY_ANOMALY,
        severity=ScannerSeverity.WARNING,
        title="放量异动",
        summary="成交量放大至阈值以上。",
        evidence=[
            ScannerEvidence(
                metric="volume_ratio", value=Decimal("3.2"), threshold=Decimal("2")
            )
        ],
        tags=["放量"],
        created_ts=NOW_MS,
        snapshot_id=snapshot,
    )


def _content() -> BriefingContent:
    return BriefingContent(
        trigger_reason="放量突破前高",
        key_levels=[
            BriefingLevelRef(label="前高", price=Decimal("101.5"), note="观察承接")
        ],
        risks=["追高风险"],
    )


async def _wait_rows(
    service: BriefingService, count: int = 1, timeout: float = 2.0
) -> list[ScannerBriefing]:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        rows = await service.list(limit=20)
        if len(rows) >= count:
            return rows
        await asyncio.sleep(0.005)
    raise AssertionError(f"简报未生成，当前 {len(await service.list(limit=20))} 条")


async def _wait_idle(service: BriefingService, timeout: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while service.pending and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.005)


async def _wait_calls(host: Host, count: int = 1, timeout: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if len(host.calls) >= count:
            return
        await asyncio.sleep(0.005)
    raise AssertionError("简报没有派发到宿主")


def _service(
    tmp_path,
    *,
    clock: Clock,
    host: Host,
    cooldown_ms: int = 300_000,
    max_concurrency: int = 1,
    rate_per_minute: float = 6.0,
    overall_timeout_s: float = 2.0,
) -> tuple[BriefingService, StateWriterQueue, list[ScannerBriefing]]:
    queue = StateWriterQueue(tmp_path / "data", now_ms=clock)
    published: list[ScannerBriefing] = []
    service = BriefingService(
        queue=queue,
        usage=UsageService(queue),
        workbench=FakeWorkbench(),
        host_url=host,
        now_ms=clock,
        publish=published.append,
        cooldown_ms=cooldown_ms,
        max_concurrency=max_concurrency,
        rate_per_minute=rate_per_minute,
        overall_timeout_s=overall_timeout_s,
        dispatcher=host.dispatch,
    )
    return service, queue, published


def test_same_instrument_cooldown_merges_without_second_dispatch(tmp_path) -> None:
    async def scenario() -> None:
        clock = Clock()
        host = Host()
        service, queue, _ = _service(
            tmp_path, clock=clock, host=host, max_concurrency=4, rate_per_minute=600
        )
        await queue.start()
        try:
            assert service.offer(_trigger(trigger_id="scn_1")) is True
            await _wait_rows(service, 1)
            assert service.offer(_trigger(trigger_id="scn_2")) is False
            assert len(host.calls) == 1
            assert host.calls[0]["snapshot_id"] == "snap_1"
            briefings = await service.list(limit=10)
            assert len(briefings) == 1
        finally:
            await service.close()
            await queue.close()

    asyncio.run(scenario())


def test_global_token_bucket_rejects_burst_then_refills(tmp_path) -> None:
    async def scenario() -> None:
        clock = Clock()
        host = Host()
        service, queue, _ = _service(
            tmp_path,
            clock=clock,
            host=host,
            cooldown_ms=0,
            max_concurrency=4,
            rate_per_minute=60,
        )
        await queue.start()
        try:
            assert service.offer(_trigger(instrument="BTC-USDT-PERP")) is True
            await _wait_calls(host, 1)
            assert service.offer(_trigger(instrument="ETH-USDT-PERP")) is False
            clock.now += 1_000
            assert service.offer(_trigger(instrument="SOL-USDT-PERP")) is True
            await _wait_calls(host, 2)
        finally:
            await service.close()
            await queue.close()

    asyncio.run(scenario())


def test_concurrency_limit_degrades_to_numeric_card(tmp_path) -> None:
    async def scenario() -> None:
        clock = Clock()
        host = Host()
        service, queue, _ = _service(
            tmp_path, clock=clock, host=host, cooldown_ms=0, max_concurrency=1
        )
        await queue.start()
        try:
            assert service.offer(_trigger(instrument="BTC-USDT-PERP")) is True
            await _wait_calls(host, 1)
            # 第一个任务尚未完成，全局并发为 1，另一标的也必须立即降级。
            assert service.offer(_trigger(instrument="ETH-USDT-PERP")) is False
        finally:
            await service.close()
            await queue.close()

    asyncio.run(scenario())


def test_solo_or_host_offline_returns_false_without_row_or_dispatch(tmp_path) -> None:
    async def scenario() -> None:
        clock = Clock()
        host = Host(url=None)
        service, queue, _ = _service(tmp_path, clock=clock, host=host)
        await queue.start()
        try:
            assert service.offer(_trigger()) is False
            assert service.offer(_trigger(instrument="ETH-USDT-PERP")) is False
            assert host.calls == []
            assert await service.list(limit=10) == []
        finally:
            await service.close()
            await queue.close()

    asyncio.run(scenario())


def test_briefing_binds_snapshot_and_ready_result_keeps_evidence(tmp_path) -> None:
    async def scenario() -> None:
        clock = Clock()
        host = Host()
        service, queue, published = _service(
            tmp_path, clock=clock, host=host, cooldown_ms=0
        )
        await queue.start()
        try:
            assert service.offer(_trigger(snapshot="snap_bound")) is True
            pending = (await _wait_rows(service, 1))[0]
            await _wait_calls(host)
            assert pending.status is BriefingStatus.PENDING
            assert pending.snapshot_id == "snap_bound"
            assert host.calls[0]["snapshot_id"] == "snap_bound"
            assert pending.id in host.calls[0]["prompt"]

            result = await service.submit_result(
                pending.id,
                BriefingResultRequest(
                    content=_content(),
                    dsh_session_id="dsh-1",
                    dsh_turn_id="3",
                ),
            )
            assert result.status is BriefingStatus.READY
            assert result.summary == _content()
            ids = {ref.evidence_id for ref in result.evidence_refs}
            assert "snapshot:snap_bound" in ids
            assert any(ref.startswith("scanner:scn_1:") for ref in ids)
            assert published[-1].status is BriefingStatus.READY
        finally:
            await service.close()
            await queue.close()

    asyncio.run(scenario())


def test_text_result_parses_json_and_rejects_unknown_evidence(tmp_path) -> None:
    async def scenario() -> None:
        clock = Clock()
        host = Host()
        service, queue, _ = _service(
            tmp_path,
            clock=clock,
            host=host,
            cooldown_ms=0,
            rate_per_minute=600,
        )
        await queue.start()
        try:
            assert service.offer(_trigger(snapshot="snap_text")) is True
            briefing = (await _wait_rows(service, 1))[0]
            await _wait_calls(host, 1)
            text = "```json\n" + json.dumps(
                {
                    "trigger_reason": "放量突破",
                    "key_levels": [{"label": "前高", "price": "101.5"}],
                    "risks": ["假突破"],
                    "evidence_ids": ["snapshot:snap_text"],
                }
            ) + "\n```"
            ready = await service.submit_result(
                briefing.id, BriefingResultRequest(text=text)
            )
            assert ready.status is BriefingStatus.READY
            assert [ref.evidence_id for ref in ready.evidence_refs] == [
                "snapshot:snap_text"
            ]
            await _wait_idle(service)
            clock.now += 1_000

            assert service.offer(_trigger(snapshot="snap_bad")) is True
            bad = (await _wait_rows(service, 2))[0]
            await _wait_calls(host, 2)
            bad_text = json.dumps(
                {
                    "trigger_reason": "放量",
                    "key_levels": [],
                    "risks": [],
                    "evidence_ids": ["level:invented"],
                }
            )
            abstained = await service.submit_result(
                bad.id, BriefingResultRequest(text=bad_text)
            )
            assert abstained.status is BriefingStatus.ABSTAIN
            assert abstained.abstain_reason == "EVIDENCE_CONFLICT"
        finally:
            await service.close()
            await queue.close()

    asyncio.run(scenario())


def test_text_abstain_is_preserved(tmp_path) -> None:
    async def scenario() -> None:
        clock = Clock()
        host = Host()
        service, queue, _ = _service(
            tmp_path,
            clock=clock,
            host=host,
            cooldown_ms=0,
            rate_per_minute=600,
        )
        await queue.start()
        try:
            assert service.offer(_trigger(snapshot="snap_abstain")) is True
            briefing = (await _wait_rows(service, 1))[0]
            await _wait_calls(host, 1)
            text = json.dumps(
                {
                    "abstain": {
                        "reason_code": "EVIDENCE_CONFLICT",
                        "message": "证据目录只有快照，无法形成可执行观察",
                        "evidence_refs": [],
                    }
                }
            )
            abstained = await service.submit_result(
                briefing.id, BriefingResultRequest(text=text)
            )
            assert abstained.status is BriefingStatus.ABSTAIN
            assert abstained.abstain_reason == "EVIDENCE_CONFLICT"
            assert abstained.summary is None
        finally:
            await service.close()
            await queue.close()

    asyncio.run(scenario())


def test_abstain_evidence_must_exist_in_catalog(tmp_path) -> None:
    async def scenario() -> None:
        clock = Clock()
        host = Host()
        service, queue, _ = _service(
            tmp_path,
            clock=clock,
            host=host,
            cooldown_ms=0,
            rate_per_minute=600,
        )
        await queue.start()
        try:
            assert service.offer(_trigger(snapshot="snap_abstain_refs")) is True
            briefing = (await _wait_rows(service, 1))[0]
            await _wait_calls(host, 1)
            text = json.dumps(
                {
                    "abstain": {
                        "reason_code": "EVIDENCE_CONFLICT",
                        "message": "只有快照证据，无法形成观察",
                        "evidence_refs": [
                            {
                                "evidence_id": "level:invented",
                                "kind": "KEY_LEVEL",
                                "label": "编造价位",
                            }
                        ],
                    }
                }
            )
            abstained = await service.submit_result(
                briefing.id, BriefingResultRequest(text=text)
            )
            assert abstained.status is BriefingStatus.ABSTAIN
            ids = {ref.evidence_id for ref in abstained.evidence_refs}
            assert "level:invented" not in ids
            assert "snapshot:snap_abstain_refs" in ids
        finally:
            await service.close()
            await queue.close()

    asyncio.run(scenario())


def test_invalid_output_and_timeout_abstain(tmp_path) -> None:
    async def scenario() -> None:
        clock = Clock()
        host = Host()
        service, queue, _ = _service(
            tmp_path,
            clock=clock,
            host=host,
            cooldown_ms=0,
            rate_per_minute=600,
            overall_timeout_s=0.03,
        )
        await queue.start()
        try:
            assert service.offer(_trigger(snapshot="snap_invalid")) is True
            invalid = (await _wait_rows(service, 1))[0]
            await _wait_calls(host)
            abstained = await service.submit_result(
                invalid.id, BriefingResultRequest(text="不是 JSON")
            )
            assert abstained.status is BriefingStatus.ABSTAIN
            assert abstained.abstain_reason == "MODEL_INVALID_OUTPUT"
            await _wait_idle(service)
            clock.now += 1_000

            assert service.offer(_trigger(snapshot="snap_timeout")) is True
            timed_out = (await _wait_rows(service, 2))[0]
            assert timed_out.snapshot_id == "snap_timeout"
            for _ in range(100):
                current = await service.get(timed_out.id)
                if current.status is BriefingStatus.ABSTAIN:
                    break
                await asyncio.sleep(0.01)
            assert current.abstain_reason == "MODEL_TIMEOUT"
        finally:
            await service.close()
            await queue.close()

    asyncio.run(scenario())


def test_usage_in_result_is_idempotent(tmp_path) -> None:
    async def scenario() -> None:
        clock = Clock()
        host = Host()
        service, queue, _ = _service(
            tmp_path, clock=clock, host=host, cooldown_ms=0
        )
        await queue.start()
        try:
            assert service.offer(_trigger()) is True
            briefing = (await _wait_rows(service, 1))[0]
            usage = UsageReport(
                dsh_session_id="dsh-1",
                dsh_turn_id="4",
                input_tokens=120,
                cached_input_tokens=30,
                output_tokens=40,
            )
            payload = BriefingResultRequest(
                content=_content(),
                dsh_session_id="dsh-1",
                dsh_turn_id="4",
                usage=usage,
            )
            await service.submit_result(briefing.id, payload)
            await service.submit_result(briefing.id, payload)
            total, _sessions = await queue.run(lambda store: store.usage.summarize())
            rows = await queue.run(lambda store: store.usage.list(limit=10))
            assert total.turns == 1
            assert rows[0].input_tokens == 120
        finally:
            await service.close()
            await queue.close()

    asyncio.run(scenario())


def test_routing_declines_to_local_and_accepts_to_copilot() -> None:
    class Channel:
        def __init__(self, accepted: bool) -> None:
            self.accepted = accepted
            self.offers = 0

        def offer(self, trigger: ScannerTrigger) -> bool:
            self.offers += 1
            return self.accepted

    accepted = Channel(True)
    policy = TriggerRoutingPolicy(co_pilot=accepted, co_pilot_enabled=True)
    assert policy.route(_trigger()) is ScannerTriggerRoute.CO_PILOT_CONSULT
    assert accepted.offers == 1

    declined = Channel(False)
    policy = TriggerRoutingPolicy(co_pilot=declined, co_pilot_enabled=True)
    assert policy.route(_trigger()) is ScannerTriggerRoute.LOCAL
    assert declined.offers == 1


def test_mark_read_and_unread_filter(tmp_path) -> None:
    async def scenario() -> None:
        clock = Clock()
        host = Host()
        service, queue, _ = _service(
            tmp_path,
            clock=clock,
            host=host,
            cooldown_ms=0,
            max_concurrency=4,
            rate_per_minute=600,
        )
        await queue.start()
        try:
            for index in range(2):
                assert service.offer(
                    _trigger(instrument=f"I{index}", trigger_id=f"scn_{index}")
                )
            await _wait_rows(service, 2)
            unread = await service.list(limit=10, unread_only=True)
            assert len(unread) == 2
            read = await service.mark_read(unread[0].id)
            assert read.read_ts == NOW_MS
            assert len(await service.list(limit=10, unread_only=True)) == 1
        finally:
            await service.close()
            await queue.close()

    asyncio.run(scenario())
