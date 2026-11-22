"""快照中用于规则回放的发布版本与同毫秒接收顺序。"""

import json
import zlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from talaria.foundation.timeframes import timeframe_ms
from talaria.foundation.feed import (
    DerivativeTickEvent, FeedEvent, FeedHealthEvent, KlineEvent, OrderBookEvent, ReceivedKlineEvent,
)


@dataclass(frozen=True, slots=True)
class SnapshotEvent:
    order: int
    event: FeedEvent

    @property
    def received_at(self) -> int:
        if isinstance(self.event, FeedHealthEvent):
            return self.event.ts
        if isinstance(self.event, KlineEvent) and not isinstance(self.event, ReceivedKlineEvent):
            bar = self.event.bar
            return max(bar.ts_available, bar.timestamp + timeframe_ms(self.event.timeframe))
        return self.event.ts_ingested


def encode_events(events: Sequence[SnapshotEvent]) -> bytes:
    rows = []
    for item in events:
        event = item.event
        payload = event.model_dump(mode="json")
        if isinstance(event, ReceivedKlineEvent):
            payload["ts_ingested"] = event.ts_ingested
        if isinstance(event, OrderBookEvent):
            kind = "OrderBookEvent"
        else:
            kind = type(event).__name__
        rows.append((item.order, kind, payload))
    return zlib.compress(json.dumps(rows, separators=(",", ":")).encode())


def decode_events(payload: bytes, check_cancelled: Callable[[], None]) -> list[SnapshotEvent]:
    models = {t.__name__: t for t in (KlineEvent, ReceivedKlineEvent, FeedHealthEvent, OrderBookEvent, DerivativeTickEvent)}
    events = []
    for order, kind, data in json.loads(zlib.decompress(payload)):
        check_cancelled()
        events.append(SnapshotEvent(order, models[kind].model_validate(data)))
    return events
