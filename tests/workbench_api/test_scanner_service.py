"""扫描服务：焦点持久化、路由占位与零模型调用。"""

from __future__ import annotations

import asyncio
import sys
from decimal import Decimal

import pytest

from talaria.foundation.bars import CompactBar
from talaria.foundation.market import InstrumentSpec
from talaria.foundation.scanner import (
    ScannerConditionKind,
    ScannerTrigger,
    ScannerTriggerRoute,
)
from talaria.foundation.types import PrecisionTier
from talaria.packs.crypto_perp import CRYPTO_PERP_PACK
from talaria.perception.scanner import ScannerConfig, TriggerRoutingPolicy
from talaria.workbench_api.scanner_service import ScannerService
from talaria.workbench_api.state_repositories.watchlist import SCANNER_FOCUS_GROUP_ID
from talaria.workbench_api.state_store import StateWriterQueue

pytestmark = pytest.mark.medium

MINUTE = 60_000
BASE_TS = 1_795_000_000_000


class Clock:
    def __init__(self, now: int = BASE_TS) -> None:
        self.now = now

    def __call__(self) -> int:
        return self.now


class SpyChannel:
    def __init__(self) -> None:
        self.offers: list[ScannerTrigger] = []

    def offer(self, trigger: ScannerTrigger) -> None:
        self.offers.append(trigger)


def _bar(ts: int, *, volume: float = 10.0) -> CompactBar:
    return CompactBar(
        timestamp=ts,
        open=Decimal("100"),
        high=Decimal("100.5"),
        low=Decimal("99.5"),
        close=Decimal("100"),
        volume=Decimal(str(volume)),
        turnover=Decimal(str(volume * 100)),
        status="NORMAL",  # type: ignore[arg-type]
        ts_available=ts + MINUTE,
        precision=PrecisionTier.SECOND_VERIFIED,
    )


def _instrument(spec: InstrumentSpec | None = None) -> InstrumentSpec:
    return spec or CRYPTO_PERP_PACK.instruments()[0]


def _freezer(calls: list[tuple[str, str, int]]) -> object:
    def freeze(instrument_id: str, timeframe: str, now: int) -> str:
        calls.append((instrument_id, timeframe, now))
        return "snap_service"

    return freeze


def _seed(service: ScannerService, instrument: InstrumentSpec, now: int) -> None:
    bars = [_bar(BASE_TS + index * MINUTE) for index in range(40)]
    bars[-1] = _bar(BASE_TS + 39 * MINUTE, volume=100.0)
    service.seed_bars(instrument.instrument_id, "1m", bars)


async def _run_service(
    tmp_path: object,
    *,
    clock: Clock | None = None,
    routing: TriggerRoutingPolicy | None = None,
    published: list[ScannerTrigger] | None = None,
    changes: list[bool] | None = None,
) -> tuple[ScannerService, Clock, StateWriterQueue]:
    clock = clock or Clock(BASE_TS + 40 * MINUTE)
    writer = StateWriterQueue(tmp_path, now_ms=clock)  # type: ignore[arg-type]
    await writer.start()
    calls: list[tuple[str, str, int]] = []
    service = ScannerService(
        state_writer=writer,
        instruments=CRYPTO_PERP_PACK.instruments(),
        now_ms=clock,
        freeze_snapshot=_freezer(calls),  # type: ignore[arg-type]
        publish=(published.append if published is not None else None),
        on_change=(lambda: changes.append(True)) if changes is not None else None,
        config=ScannerConfig(),
        routing=routing,
    )
    _seed(service, _instrument(), clock.now)
    return service, clock, writer


def test_scan_publishes_focus_group_and_keeps_user_groups(tmp_path: object) -> None:
    async def scenario() -> None:
        published: list[ScannerTrigger] = []
        changes: list[bool] = []
        service, clock, writer = await _run_service(
            tmp_path, published=published, changes=changes
        )
        try:
            await writer.run(
                lambda store: store.watchlist.upsert_system_group("扫描焦点", [])
            )
            await writer.run(
                lambda store: store.watchlist.create_group(
                    "我的自选", group_id="manual-1"
                )
            )

            triggers = await service.scan()
            assert triggers
            assert len(published) == len(triggers)
            assert changes == [True]

            records = await writer.run(lambda store: store.watchlist.list())
            focus = [group for group in records.groups if group.group_id == SCANNER_FOCUS_GROUP_ID]
            assert len(focus) == 1
            assert focus[0].kind == "SCANNER_FOCUS"
            assert focus[0].is_system is True
            items = records.items_for(SCANNER_FOCUS_GROUP_ID)
            assert {item.instrument_id for item in items} == {
                trigger.instrument_id for trigger in triggers
            }
            manual = [group for group in records.groups if group.group_id == "manual-1"]
            assert len(manual) == 1

            response = service.focus()
            assert response.group_id == SCANNER_FOCUS_GROUP_ID
            assert response.instrument_ids == [item.instrument_id for item in items]
            assert response.items[0].trigger_count >= 1
            assert response.items[0].last_snapshot_id == "snap_service"
        finally:
            await writer.close()

    asyncio.run(scenario())


def test_empty_focus_does_not_create_system_group(tmp_path: object) -> None:
    async def scenario() -> None:
        clock = Clock(BASE_TS + 40 * MINUTE)
        writer = StateWriterQueue(tmp_path, now_ms=clock)  # type: ignore[arg-type]
        await writer.start()
        service = ScannerService(
            state_writer=writer,
            instruments=CRYPTO_PERP_PACK.instruments(),
            now_ms=clock,
            freeze_snapshot=_freezer([]),  # type: ignore[arg-type]
            config=ScannerConfig(),
        )
        try:
            assert await service.scan() == []
            records = await writer.run(lambda store: store.watchlist.list())
            assert records.groups == ()
            assert service.focus().instrument_ids == []
        finally:
            await writer.close()

    asyncio.run(scenario())


def test_focus_ttl_removes_item_but_keeps_system_group(tmp_path: object) -> None:
    async def scenario() -> None:
        service, clock, writer = await _run_service(tmp_path)
        try:
            triggers = await service.scan()
            assert triggers
            focus_id = triggers[0].instrument_id
            clock.now += ScannerConfig().focus_ttl_ms + 1
            assert await service.scan() == []
            records = await writer.run(lambda store: store.watchlist.list())
            group = next(
                group
                for group in records.groups
                if group.group_id == SCANNER_FOCUS_GROUP_ID
            )
            assert group.is_system is True
            assert records.items_for(SCANNER_FOCUS_GROUP_ID) == ()
            assert service.focus().items == []
            assert focus_id not in service.focus().instrument_ids
        finally:
            await writer.close()

    asyncio.run(scenario())


def test_local_route_never_touches_co_pilot_channel(tmp_path: object) -> None:
    async def scenario() -> None:
        channel = SpyChannel()
        routing = TriggerRoutingPolicy(co_pilot=channel)
        service, _, writer = await _run_service(tmp_path, routing=routing)
        try:
            triggers = await service.scan()
            assert triggers
            assert all(trigger.route is ScannerTriggerRoute.LOCAL for trigger in triggers)
            assert channel.offers == []
        finally:
            await writer.close()

    asyncio.run(scenario())


def test_explicit_placeholder_route_offers_without_network(tmp_path: object) -> None:
    async def scenario() -> None:
        channel = SpyChannel()
        routing = TriggerRoutingPolicy(co_pilot=channel, co_pilot_enabled=True)
        service, _, writer = await _run_service(tmp_path, routing=routing)
        try:
            triggers = await service.scan()
            assert triggers
            assert all(
                trigger.route is ScannerTriggerRoute.CO_PILOT_CONSULT
                for trigger in triggers
            )
            assert len(channel.offers) == len(triggers)
        finally:
            await writer.close()

    asyncio.run(scenario())


def test_scanner_has_no_model_client_dependency() -> None:
    for module in ("openai", "anthropic", "litellm", "talaria.cognition"):
        assert module not in sys.modules
    import talaria.perception.scanner.engine as engine
    import talaria.workbench_api.scanner_service as service

    for candidate in (engine, service):
        source = candidate.__file__ or ""
        assert "openai" not in source
        for name in dir(candidate):
            assert "client" not in name.lower()
    assert ScannerConditionKind.KEY_LEVEL_EVENT in {
        kind for kind in ScannerConditionKind
    }


def test_manual_rule_only_offers_original_gate_hits(tmp_path):
    from talaria.foundation.rules import Rule

    async def scenario():
        channel = SpyChannel()
        service, clock, writer = await _run_service(tmp_path, routing=TriggerRoutingPolicy(
            co_pilot=channel, co_pilot_enabled=True))
        try:
            spec = _instrument()
            rule = Rule.model_validate(dict(version="1", timing="bar_close", expression=dict(
                op="compare", test="gt", left=dict(source="bar", field="close"),
                right=dict(source="constant", value="0"))))
            # 前一根无成交额异常。最后一根已预灌，下一分钟才可见。
            clock.now -= MINUTE
            first, = await service.scan([spec.instrument_id], rule=rule)
            assert first.route is ScannerTriggerRoute.LOCAL
            assert not channel.offers
            assert first in service.list_triggers()
            clock.now += MINUTE
            # 换一条同样命中的规则，正常门禁仍只接收一次。
            rule.expression.right.value = Decimal("1")
            await service.scan([spec.instrument_id], rule=rule)
            assert [t.kind for t in channel.offers] == [ScannerConditionKind.LIQUIDITY_ANOMALY]
            await service.scan([spec.instrument_id])
            assert len(channel.offers) == 1
        finally:
            await writer.close()
    asyncio.run(scenario())
