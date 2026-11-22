"""指标默认值：服务端按指标名保存用户的参数覆盖。

覆盖值写入状态库 meta 表（键 ``indicator_defaults.v1``），未覆盖时回落到
Pine v6 对齐的工厂默认值。覆盖值的读改写由 ``StateWriterQueue`` 串行执行。
"""

from __future__ import annotations

from talaria.foundation.indicators import IndicatorParams
from talaria.foundation.operators import IndicatorName, spec_from_params
from talaria.perception.indicator_defaults import (
    DEFAULTS_KEY,
    effective_defaults,
    factory_defaults,
    params_from_spec,
    parse_overrides,
)
from talaria.workbench_api.state_store import StateStore, StateWriterQueue


class IndicatorDefaultsService:
    """生效默认值的读写入口；无缓存，避免多进程/多实例间不一致。"""

    def __init__(self, state_writer: StateWriterQueue) -> None:
        self._state_writer = state_writer

    async def effective(self) -> dict[IndicatorName, IndicatorParams]:
        return effective_defaults(await self._overrides())

    async def get(self, name: IndicatorName) -> IndicatorParams:
        name = IndicatorName(name)
        overrides = await self._overrides()
        return overrides.get(name) or factory_defaults(name)

    async def set(
        self, name: IndicatorName, params: IndicatorParams
    ) -> IndicatorParams:
        name = IndicatorName(name)
        spec = spec_from_params(name, params.model_dump(exclude_none=True))
        canonical = params_from_spec(spec)
        await self._state_writer.run(lambda store: self._update(store, name, canonical))
        return canonical

    async def clear(self, name: IndicatorName) -> IndicatorParams:
        name = IndicatorName(name)
        await self._state_writer.run(lambda store: self._update(store, name, None))
        return factory_defaults(name)

    async def _overrides(self) -> dict[IndicatorName, IndicatorParams]:
        return parse_overrides(await self._state_writer.get_meta(DEFAULTS_KEY))

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
