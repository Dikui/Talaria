"""策略实时订阅客户端，断线时重新发现常驻服务并补发。"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncGenerator
from pathlib import Path
from uuid import uuid4

from httpx import AsyncClient, HTTPStatusError, TransportError
from talaria.foundation.bars import BarsResponse, RangeBar
from talaria.foundation.errors import InvalidRequestError, TalariaError
from talaria.foundation.strategy_subscription import (
    StrategyBarEvent,
    SubscriptionCursor,
    SubscriptionReady,
    SubscriptionRequest,
)
from talaria.foundation.timeframes import SUPPORTED_TIMEFRAMES, last_closed_bar_start, timeframe_ms
from talaria.service_endpoint import DATA_DIR_HEADER, data_dir_id, read_service_connection
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus


class SubscriptionRejectedError(TalariaError):
    """常驻服务拒绝策略订阅，例如实时订阅配额已满。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


async def _range_revisions(
    base_url: str, directory_id: str, request: SubscriptionRequest, *,
    since: int, now: int, interval: int,
) -> AsyncGenerator[RangeBar]:
    end = last_closed_bar_start(now, interval)
    if end < 0:
        return
    params = {
        "instrument": request.instrument, "timeframe": request.timeframe,
        "as_of": now, "available_since": since,
    }
    ranges = [(0, end)]
    async with AsyncClient(base_url=base_url, trust_env=False, timeout=35) as client:
        while ranges:
            start, end = ranges.pop()
            response = await client.get("/api/bars", params={**params, "start": start, "end": end})
            if response.status_code == 400 and end > start:
                if response.json().get("code") == "BARS_RANGE_TOO_LARGE":
                    mid = (start + end) // 2
                    ranges.extend(((mid + 1, end), (start, mid)))
                    continue
            response.raise_for_status()
            payload = BarsResponse.model_validate_json(response.content)
            if (
                response.headers.get(DATA_DIR_HEADER) != directory_id
                or (payload.instrument_id, payload.timeframe) != (request.instrument, request.timeframe)
            ):
                raise TransportError("重同步响应的数据目录、标的或周期与订阅不一致")
            for bar in payload.bars:
                if bar.confirmed:
                    yield bar


async def subscribe(
    instrument: str, timeframe: str, *, data_dir: str | Path | None = None,
) -> AsyncGenerator[StrategyBarEvent]:
    """订阅已收盘柱修订，断线时自动重连。

    数据目录与 ``bars()`` 的解析规则相同。主动停止消费时可用
    ``contextlib.aclosing`` 及时释放本次订阅。常驻服务明确拒绝时抛出
    ``SubscriptionRejectedError``。
    """
    if timeframe not in SUPPORTED_TIMEFRAMES:
        raise InvalidRequestError(f"不支持的周期：{timeframe}")
    root = Path(data_dir or os.environ.get("TALARIA_DATA_DIR") or ".talaria").expanduser().resolve()
    request = SubscriptionRequest(instrument=instrument, timeframe=timeframe, owner=uuid4().hex)
    directory_id = data_dir_id(root)
    interval = timeframe_ms(timeframe)
    cursor: SubscriptionCursor | None = None
    since: int | None = None
    seen: set[tuple[int, int]] = set()
    delay = 0.1
    while True:
        endpoint = read_service_connection(root)
        if endpoint is not None:
            try:
                async with connect(
                    endpoint.base_url.replace("http://", "ws://", 1) + "/ws/strategy",
                    additional_headers={
                        "Authorization": f"Bearer {endpoint.token}", DATA_DIR_HEADER: directory_id,
                    },
                    proxy=None, open_timeout=2, close_timeout=1,
                ) as socket:
                    await socket.send(request.model_copy(update={"after": cursor}).model_dump_json())
                    answer = json.loads(await asyncio.wait_for(socket.recv(), timeout=10))
                    if answer.get("type") == "error":
                        raise SubscriptionRejectedError(answer["code"], answer["message"])
                    ready = SubscriptionReady.model_validate(answer)
                    if since is None:
                        # 重取窗口覆盖订阅时刚收盘、确认尚在途中的柱。
                        since = max(0, ready.started_ts - interval)
                    if ready.type == "resync":
                        try:
                            async for bar in _range_revisions(
                                endpoint.base_url, directory_id, request, since=since,
                                now=ready.server_ts, interval=interval,
                            ):
                                identity = (bar.ts_event, bar.revision)
                                if identity not in seen:
                                    seen.add(identity)
                                    yield StrategyBarEvent(
                                        instrument_id=instrument, timeframe=timeframe,
                                        epoch=ready.epoch, seq=None, bar=bar,
                                    )
                        except (HTTPStatusError, TransportError):
                            await socket.close(code=1012)
                            raise
                    cursor = SubscriptionCursor(epoch=ready.epoch, seq=ready.seq)
                    delay = 0.1
                    while True:
                        event = StrategyBarEvent.model_validate_json(await socket.recv())
                        if event.epoch != cursor.epoch or event.seq is None or event.seq > cursor.seq + 1:
                            await socket.close(code=1012)
                            break
                        if event.seq <= cursor.seq:
                            continue
                        cursor = SubscriptionCursor(epoch=event.epoch, seq=event.seq)
                        identity = (event.bar.ts_event, event.bar.revision)
                        if identity not in seen:
                            seen.add(identity)
                            yield event
            except (ConnectionClosed, TimeoutError, OSError, TransportError):
                pass
            except HTTPStatusError as exc:
                if exc.response.status_code < 500:
                    raise
            except InvalidStatus as exc:
                if exc.response.status_code not in (401, 403, 502, 503, 504):
                    raise
        await asyncio.sleep(delay)
        delay = min(delay * 2, 5)
