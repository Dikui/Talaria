"""Trigger 路由策略：本任务只实现本地工作台提醒。

``CO_PILOT_CONSULT`` 只保留一个同步、无副作用的接口。生产装配不会传入
实现，因此扫描路径不会发起模型调用或网络请求；后续任务单可注入真正的
协同会诊通道，而无需改扫描器。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from talaria.foundation.scanner.contracts import (
    ScannerConditionKind,
    ScannerTrigger,
    ScannerTriggerRoute,
)


@runtime_checkable
class CoPilotConsultChannel(Protocol):
    """协同会诊通道的占位接口；实现方自行承担异步与限额。"""

    def offer(self, trigger: ScannerTrigger) -> bool | None:
        """尝试为一次命中申请协同简报。

        返回 ``False`` 表示冷却/限流/宿主离线，路由降级为纯数值卡；返回
        ``None`` 或 ``True`` 保持兼容，视为已接受。
        """


class TriggerRoutingPolicy:
    """按装配决定 LOCAL 或 CO_PILOT_CONSULT；默认恒为 LOCAL。"""

    def __init__(
        self,
        *,
        co_pilot: CoPilotConsultChannel | None = None,
        co_pilot_enabled: bool = False,
    ) -> None:
        if co_pilot_enabled and co_pilot is None:
            raise ValueError("启用 CO_PILOT_CONSULT 时必须提供通道实现")
        self._co_pilot = co_pilot
        self._co_pilot_enabled = co_pilot_enabled

    @property
    def co_pilot_enabled(self) -> bool:
        return self._co_pilot_enabled

    def route(self, trigger: ScannerTrigger) -> ScannerTriggerRoute:
        """返回路由并（仅在显式启用时）把候选交给占位通道。"""

        if trigger.kind is ScannerConditionKind.RULE:
            return ScannerTriggerRoute.LOCAL
        if self._co_pilot_enabled and self._co_pilot is not None:
            accepted = self._co_pilot.offer(trigger)
            if accepted is False:
                return ScannerTriggerRoute.LOCAL
            return ScannerTriggerRoute.CO_PILOT_CONSULT
        return ScannerTriggerRoute.LOCAL
