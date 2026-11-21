"""指标默认值：服务端按指标名保存用户的参数覆盖。

覆盖值写入状态库 meta 表（键 ``indicator_defaults.v1``），未覆盖时回落到
Pine v6 对齐的工厂默认值。所有读取都经 ``StateWriterQueue``，因此与预警
迁移、规则写入共享同一串行写入顺序。
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
from talaria.workbench_api.state_store import StateWriterQueue

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
        overrides = await self._overrides()
        overrides[name] = canonical
        await self._save(overrides)
        return canonical

    async def clear(self, name: IndicatorName) -> IndicatorParams:
        name = IndicatorName(name)
        overrides = await self._overrides()
        if name in overrides:
            del overrides[name]
            await self._save(overrides)
        return factory_defaults(name)

    async def _overrides(self) -> dict[IndicatorName, IndicatorParams]:
        raw = await self._state_writer.get_meta(DEFAULTS_KEY)
        if not isinstance(raw, dict):
            return {}
        overrides: dict[IndicatorName, IndicatorParams] = {}
        for key, value in raw.items():
            try:
                name = IndicatorName(key)
                params = IndicatorParams.model_validate(value)
                spec_from_params(name, params.model_dump(exclude_none=True))
            except (ValueError, TypeError):
                continue
            overrides[name] = _params_from_spec(
                spec_from_params(name, params.model_dump(exclude_none=True))
            )
        return overrides

    async def _save(self, overrides: dict[IndicatorName, IndicatorParams]) -> None:
        payload: dict[str, Any] = {
            name.value: params.model_dump(mode="json")
            for name, params in overrides.items()
        }
        await self._state_writer.set_meta(DEFAULTS_KEY, payload)


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
