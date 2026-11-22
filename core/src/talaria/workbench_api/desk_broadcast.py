"""工作台 WS 的有界广播与关键位投影。"""

from __future__ import annotations

import asyncio
from contextlib import suppress

from talaria.foundation.alerts import AlertTriggerRecord
from talaria.foundation.briefings import ScannerBriefing
from talaria.foundation.key_levels import KeyLevelSet
from talaria.foundation.scanner import ScannerTrigger

type DeskKey = tuple[str, str]
type DeskQueue = asyncio.Queue[dict[str, object]]

WS_QUEUE_MAX = 256


def enqueue(queue: DeskQueue, payload: dict[str, object]) -> None:
    """队列满时丢弃最旧事件，慢客户端不会阻塞其他人。"""
    if queue.full():
        with suppress(asyncio.QueueEmpty):
            queue.get_nowait()
    queue.put_nowait(payload)


def levels_payload(level_set: KeyLevelSet) -> dict[str, object]:
    return {"type": "levels", "data": level_set.model_dump(mode="json")}


class DeskBroadcast:
    def __init__(self) -> None:
        self.chart_subscribers: dict[DeskKey, int] = {}
        self._clients: dict[DeskQueue, set[DeskKey]] = {}

    def attach(self, filters: set[DeskKey]) -> DeskQueue:
        queue: DeskQueue = asyncio.Queue(maxsize=WS_QUEUE_MAX)
        self._clients[queue] = filters
        return queue

    def detach(self, queue: DeskQueue) -> None:
        self._clients.pop(queue, None)

    def project_levels(self, level_set: KeyLevelSet) -> None:
        key = (level_set.instrument_id, level_set.timeframe)
        payload = levels_payload(level_set)
        for queue, filters in self._clients.items():
            if key in filters:
                enqueue(queue, payload)

    def publish_alert(self, trigger: AlertTriggerRecord) -> None:
        self._broadcast({"type": "alert", "data": trigger.model_dump(mode="json")})

    def publish_scanner(self, trigger: ScannerTrigger) -> None:
        self._broadcast({"type": "scanner", "data": trigger.model_dump(mode="json")})

    def publish_briefing(self, briefing: ScannerBriefing) -> None:
        self._broadcast({"type": "briefing", "data": briefing.model_dump(mode="json")})

    def _broadcast(self, payload: dict[str, object]) -> None:
        for queue in self._clients:
            enqueue(queue, payload)
