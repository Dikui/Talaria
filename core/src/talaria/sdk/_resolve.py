"""SDK 数据目录与标的规格解析辅助（在线服务优先，其次本地清单，最后回退默认市场包）。"""

from __future__ import annotations

from pathlib import Path

import httpx

from talaria.foundation.errors import InstrumentNotFoundError
from talaria.foundation.market import InstrumentSpec
from talaria.packs import DEFAULT_PACK, get_market_pack
from talaria.packs.catalog import fetch_online_instruments as _fetch_online_instruments
from talaria.perception.cold_store import ColdReader
from talaria.service_endpoint import data_dir_id, default_data_dir, read_service_endpoint


def resolve_instrument_spec(
    instrument: str,
    *,
    root: Path,
    endpoint: str | None = None,
    directory_id: str | None = None,
    offline: bool = False,
    reader: ColdReader | None = None,
    client_cls: type[httpx.Client] = httpx.Client,
) -> InstrumentSpec:
    """按在线服务 -> 本地清单 -> （离线或旧服务回退）Python 默认市场包顺序解析标的规格。"""
    online: list[InstrumentSpec] | None = None
    if not offline and endpoint is not None:
        online = _fetch_online_instruments(endpoint, directory_id, client_cls=client_cls)
        if online is not None:
            for spec in online:
                if spec.instrument_id == instrument:
                    return spec

    active_reader = reader if reader is not None else ColdReader(root)
    local_spec = active_reader.resolve_instrument(instrument)
    if local_spec is not None:
        return local_spec

    if online is not None:
        raise InstrumentNotFoundError(f"在线服务未配置标的：{instrument}")

    fallback = get_market_pack(DEFAULT_PACK).instrument(instrument)
    if fallback is not None:
        return fallback
    raise InstrumentNotFoundError(f"未知标的：{instrument}")


def list_instruments(
    *,
    data_dir: str | Path | None = None,
    offline: bool = False,
    reader: ColdReader | None = None,
    client_cls: type[httpx.Client] = httpx.Client,
) -> list[InstrumentSpec]:
    """列出当前数据目录或在线服务中的可用标的规格。"""
    root = default_data_dir(data_dir)
    if not offline:
        endpoint = read_service_endpoint(root)
        if endpoint is not None:
            online = _fetch_online_instruments(endpoint, data_dir_id(root), client_cls=client_cls)
            if online is not None:
                return online
    active_reader = reader if reader is not None else ColdReader(root)
    local_specs = active_reader.list_instrument_specs()
    if local_specs:
        return local_specs
    return list(get_market_pack(DEFAULT_PACK).instruments())
