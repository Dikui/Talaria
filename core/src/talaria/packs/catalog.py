"""基于在线服务、冷库清单与回退包解析标的与市场能力。"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from decimal import Decimal
from pathlib import Path

import httpx

from talaria.foundation.connector import MarketDataConnector
from talaria.foundation.feed import FeedTransport
from talaria.foundation.market import (
    CarryTrait,
    FeedCapabilityTrait,
    InstrumentSpec,
    MarginTrait,
    MarketPack,
    OrderConstraintTrait,
    SessionTrait,
    TradingSession,
    TraitBundle,
)
from talaria.packs import DEFAULT_PACK, get_market_pack
from talaria.packs.crypto_perp.instruments import _TRAITS as _CRYPTO_PERP_TRAITS
from talaria.packs.crypto_perp.margin import CryptoPerpMarginValuation
from talaria.perception.cold_store import ColdReader
from talaria.service_endpoint import DATA_DIR_HEADER, data_dir_id, default_data_dir, read_service_endpoint

_FEED_ALL = FeedCapabilityTrait(realtime=True, polling=True, history=True)
_SESSION_24X7 = SessionTrait(
    session_kind="24x7",
    timezone="UTC",
    day_start="00:00",
    sessions=(
        TradingSession(name="asia", start="00:00", end="08:00"),
        TradingSession(name="london", start="07:00", end="16:00"),
        TradingSession(name="new_york", start="12:00", end="21:00"),
    ),
)
_SPOT_TRAITS = TraitBundle(
    feed=_FEED_ALL,
    margin=MarginTrait(margin_asset="USDT", max_leverage=Decimal("1")),
    carry=CarryTrait(funding_interval_hours=None),
    session=_SESSION_24X7,
    order_constraint=OrderConstraintTrait(post_only=True),
    derivatives=None,
)
_US_EQUITY_TRAITS = TraitBundle(
    feed=_FEED_ALL,
    margin=MarginTrait(margin_asset="USD", max_leverage=Decimal("4")),
    carry=CarryTrait(funding_interval_hours=None),
    session=SessionTrait(
        session_kind="regular",
        timezone="America/New_York",
        day_start="09:30",
        sessions=(
            TradingSession(name="regular", start="09:30", end="16:00", timezone="America/New_York"),
        ),
    ),
    order_constraint=OrderConstraintTrait(post_only=False),
    derivatives=None,
)
_TRAITS_BY_ASSET_CLASS: dict[str, TraitBundle] = {
    "crypto_perp": _CRYPTO_PERP_TRAITS,
    "crypto_spot": _SPOT_TRAITS,
    "us_equity": _US_EQUITY_TRAITS,
}


def traits_for_spec(spec: InstrumentSpec) -> TraitBundle:
    """按标的资产类别返回市场能力组合；未知类别回退为 24x7 1x 现货能力。"""
    return _TRAITS_BY_ASSET_CLASS.get(spec.asset_class, _SPOT_TRAITS)


def fetch_online_instruments(
    endpoint: str,
    directory_id: str | None,
    *,
    client_cls: type[httpx.Client] = httpx.Client,
) -> list[InstrumentSpec] | None:
    """从在役常驻服务的 ``/api/instruments`` 拉取已验证数据目录的标的清单。"""
    try:
        with client_cls(
            base_url=endpoint, trust_env=False, timeout=httpx.Timeout(2, connect=1)
        ) as client:
            resp = client.get("/api/instruments")
            if resp.status_code != 200:
                return None
            if directory_id is not None:
                header_dir = resp.headers.get(DATA_DIR_HEADER)
                if header_dir is not None:
                    if header_dir != directory_id:
                        return None
                else:
                    health = client.get("/api/health", timeout=1)
                    if not health.is_success or health.headers.get(DATA_DIR_HEADER) != directory_id:
                        return None
            payload = resp.json()
            if not isinstance(payload, list):
                return None
            return [InstrumentSpec.model_validate(item) for item in payload]
    except (httpx.TransportError, ValueError):
        return None


class CatalogMarketPack:
    """基于在线目录、本地冷库清单与基础回退包解析标的与能力。"""

    def __init__(
        self,
        specs: Iterable[InstrumentSpec] = (),
        *,
        base: MarketPack | None = None,
        reader: ColdReader | None = None,
        include_base_fallback: bool = True,
    ) -> None:
        resolved_base = base if base is not None else get_market_pack(DEFAULT_PACK)
        self._base = resolved_base
        self._reader = reader
        self.name = resolved_base.name
        self.raw_archive_id = resolved_base.raw_archive_id
        self.market_skill = resolved_base.market_skill
        self.default_fixture_dir = resolved_base.default_fixture_dir
        by_id: dict[str, InstrumentSpec] = {}
        for spec in specs:
            by_id.setdefault(spec.instrument_id, spec)
        if include_base_fallback:
            for spec in resolved_base.instruments():
                by_id.setdefault(spec.instrument_id, spec)
        self._by_id = by_id

    def instruments(self) -> Sequence[InstrumentSpec]:
        return tuple(self._by_id.values())

    def instrument(self, instrument_id: str) -> InstrumentSpec | None:
        spec = self._by_id.get(instrument_id)
        if spec is not None:
            return spec
        if self._reader is not None:
            spec = self._reader.resolve_instrument(instrument_id)
            if spec is not None:
                self._by_id[instrument_id] = spec
                return spec
        return None

    def traits(self, instrument_id: str) -> TraitBundle:
        spec = self.instrument(instrument_id)
        if spec is None:
            raise KeyError(instrument_id)
        return traits_for_spec(spec)

    def margin_valuation(self) -> CryptoPerpMarginValuation:
        return CryptoPerpMarginValuation(lambda inst_id: self.traits(inst_id).margin)

    def create_history_connector(
        self, *, base_url: str | None = None
    ) -> MarketDataConnector:
        return self._base.create_history_connector(base_url=base_url)

    def create_realtime_stream(self, *, now_ms: Callable[[], int]) -> FeedTransport:
        return self._base.create_realtime_stream(now_ms=now_ms)


def resolve_market_pack(
    data_dir: str | Path | None = None,
    *,
    reader: ColdReader | None = None,
    offline: bool = False,
    base_pack: str = DEFAULT_PACK,
    client_cls: type[httpx.Client] = httpx.Client,
) -> CatalogMarketPack:
    """按在线服务 -> 本地冷库清单 -> 基础市场包（仅当无在线权威目录时）顺序解析市场包。"""
    root = default_data_dir(data_dir)
    base = get_market_pack(base_pack)
    online: list[InstrumentSpec] | None = None
    if not offline:
        endpoint = read_service_endpoint(root)
        if endpoint is not None:
            online = fetch_online_instruments(endpoint, data_dir_id(root), client_cls=client_cls)
    active_reader = reader if reader is not None else ColdReader(root)
    specs: list[InstrumentSpec] = []
    if online is not None:
        specs.extend(online)
    specs.extend(active_reader.list_instrument_specs())
    return CatalogMarketPack(
        specs,
        base=base,
        reader=active_reader,
        include_base_fallback=(online is None),
    )


__all__ = [
    "CatalogMarketPack",
    "fetch_online_instruments",
    "resolve_market_pack",
    "traits_for_spec",
]
