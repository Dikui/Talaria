"""指标默认值：服务端按指标名保存用户的参数覆盖。

覆盖值写入状态库 meta 表（键 ``indicator_defaults.v1``），未覆盖时回落到
Pine v6 对齐的工厂默认值。覆盖值的读改写由 ``StateWriterQueue`` 串行执行。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from talaria.foundation.indicators import IndicatorParams
from talaria.foundation.operators import (
    IndicatorName,
    IndicatorSpec,
    spec_from_params,
)
from talaria.workbench_api.state_store import StateStore, StateWriterQueue

DEFAULTS_KEY = "indicator_defaults.v1"


class IndicatorDefaultsService:
    """生效默认值的读写入口；无缓存，避免多进程/多实例间不一致。"""

    def __init__(self, state_writer: StateWriterQueue) -> None:
        self._state_writer = state_writer

    async def effective(self) -> dict[IndicatorName, IndicatorParams]:
        overrides = await self._overrides()
        return {
            name: overrides.get(name) or factory_defaults(name)
            for name in IndicatorName
        }

    async def get(self, name: IndicatorName) -> IndicatorParams:
        name = IndicatorName(name)
        overrides = await self._overrides()
        return overrides.get(name) or factory_defaults(name)

    async def set(
        self, name: IndicatorName, params: IndicatorParams
    ) -> IndicatorParams:
        name = IndicatorName(name)
        spec = spec_from_params(name, params.model_dump(exclude_none=True))
        canonical = _params_from_spec(spec)
        await self._state_writer.run(lambda store: self._update(store, name, canonical))
        return canonical

    async def clear(self, name: IndicatorName) -> IndicatorParams:
        name = IndicatorName(name)
        await self._state_writer.run(lambda store: self._update(store, name, None))
        return factory_defaults(name)

    async def _overrides(self) -> dict[IndicatorName, IndicatorParams]:
        return _parse_overrides(await self._state_writer.get_meta(DEFAULTS_KEY))

    @staticmethod
    def _update(
        store: StateStore, name: IndicatorName, params: IndicatorParams | None
    ) -> None:
        raw = store.meta.get(DEFAULTS_KEY)
        payload = raw if isinstance(raw, dict) else {}
        if params is None:
            if name.value not in payload:
                return
            del payload[name.value]
        else:
            payload[name.value] = params.model_dump(mode="json")
        store.meta.set(DEFAULTS_KEY, payload)


def _parse_overrides(raw: Any) -> dict[IndicatorName, IndicatorParams]:
    if not isinstance(raw, dict):
        return {}
    overrides: dict[IndicatorName, IndicatorParams] = {}
    for key, value in raw.items():
        try:
            name = IndicatorName(key)
            params = IndicatorParams.model_validate(value)
            spec = spec_from_params(name, params.model_dump(exclude_none=True))
        except (ValueError, TypeError):
            continue
        overrides[name] = _params_from_spec(spec)
    return overrides


def factory_defaults(name: IndicatorName) -> IndicatorParams:
    """Pine v6 对齐的工厂默认值；BOLL 倍数显式给出以便前端展示。"""
    params = IndicatorParams.defaults_for(name)
    if name is IndicatorName.BOLL:
        return params.model_copy(update={"std_dev": Decimal("2")})
    return params


def _params_from_spec(spec: IndicatorSpec) -> IndicatorParams:
    return IndicatorParams(
        period=spec.period,
        std_dev=spec.std_dev,
        anchor=spec.anchor,
    )
