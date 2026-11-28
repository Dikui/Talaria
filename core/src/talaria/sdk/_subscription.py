"""策略实时订阅客户端，断线时重新发现常驻服务并补发。"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncGenerator, Callable
from pathlib import Path
from typing import cast
from uuid import uuid4

from httpx import AsyncClient, HTTPStatusError, TransportError
from talaria.foundation.bars import BarsResponse, RangeBar
from talaria.foundation.derivatives import DerivativeKind
from talaria.foundation.errors import InstrumentNotFoundError, InvalidRequestError, TalariaError
from talaria.foundation.strategy_subscription import (
    StrategyBarEvent,
    StrategyDerivativeEvent,
    SubscriptionCursor,
    SubscriptionReady,
    SubscriptionRequest,
    identified_derivative_revisions,
    identified_revisions,
)
from talaria.foundation.timeframes import SUPPORTED_TIMEFRAMES, last_closed_bar_start, timeframe_ms
from talaria.perception.cold_store import ColdReader
from talaria.sdk._resolve import default_data_dir, resolve_instrument_spec
from talaria.service_endpoint import DATA_DIR_HEADER, NOW_MS_HEADER, data_dir_id, read_service_connection
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus


class SubscriptionRejectedError(TalariaError):
    """常驻服务拒绝策略订阅，例如实时订阅配额已满。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


async def _range_revisions(
    base_url: str, directory_id: str, request: SubscriptionRequest, *,
    coverage_start: int, now: int, interval: int, seen: set[str], range_start: int = 0,
) -> list[tuple[str, RangeBar]]:
    result: list[tuple[str, RangeBar]] = []
    end = last_closed_bar_start(now, interval)
    if end < 0 or end < range_start:
        return result
    params = {
        "instrument": request.instrument, "timeframe": request.timeframe,
        "resync_from": coverage_start,
    }
    ranges = [(range_start, end)]
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
            confirmed = [bar for bar in payload.bars if bar.confirmed]
            covered = {bar.ts_event for bar in confirmed}
            first = (max(start, coverage_start) + interval - 1) // interval * interval
            if not covered.issuperset(range(first, end + 1, interval)):
                raise TransportError("重同步响应缺少已确认柱")
            result.extend((key, bar) for key, bar in identified_revisions(confirmed) if key not in seen)
    return result


def subscribe(
    instrument: str, timeframe: str, *, data_dir: str | Path | None = None,
    on_connection: Callable[[bool], None] | None = None,
    after: SubscriptionCursor | None = None, resume_from: int | None = None,
    on_cursor: Callable[[SubscriptionCursor, int], None] | None = None,
) -> AsyncGenerator[StrategyBarEvent]:
    """订阅已收盘柱修订，断线时自动重连。

    重同步按 ts_event 升序交付，同一时间槽按权威修订顺序交付。
    初次握手的序号与内容基线界定订阅起点；覆盖不完整时从原游标重试。
    数据目录与 ``bars()`` 的解析规则相同。主动停止消费时可用
    ``contextlib.aclosing`` 及时释放本次订阅。常驻服务明确拒绝时抛出
    ``SubscriptionRejectedError``。
    """
    return cast(AsyncGenerator[StrategyBarEvent], _subscribe(
        instrument, timeframe, data_dir=data_dir, on_connection=on_connection,
        after=after, resume_from=resume_from, on_cursor=on_cursor,
    ))


def subscribe_derivatives(
    instrument: str, kind: DerivativeKind | str, *, data_dir: str | Path | None = None,
    on_connection: Callable[[bool], None] | None = None,
    after: SubscriptionCursor | None = None, resume_from: int | None = None,
    on_cursor: Callable[[SubscriptionCursor, int], None] | None = None,
) -> AsyncGenerator[StrategyDerivativeEvent]:
    """订阅归档后的衍生品修订。日志过期后从本地清单补齐已发布的修订。"""
    return cast(AsyncGenerator[StrategyDerivativeEvent], _subscribe(
        instrument, "1m", data_dir=data_dir, kind=DerivativeKind(kind), on_connection=on_connection,
        after=after, resume_from=resume_from, on_cursor=on_cursor,
    ))


def _recover_derivatives(
    root: Path, instrument: str, kind: DerivativeKind, ready: SubscriptionReady, seen: set[str], *, as_of: int,
) -> list[StrategyDerivativeEvent]:
    reader = ColdReader(root)
    spec = resolve_instrument_spec(instrument, root=root, offline=True, reader=reader)
    points = reader.read_derivative_revisions(spec, kind, end_ts=as_of)
    return [
        StrategyDerivativeEvent(epoch=ready.epoch, seq=None, point=point, content_id=identity)
        for identity, point in identified_derivative_revisions(
            point for point in points if point.ts_available <= as_of
        )
        if identity not in seen
    ]


async def _refresh_settlements(
    base_url: str, directory_id: str, instrument: str, start: int, end: int,
) -> int:
    async with AsyncClient(base_url=base_url, trust_env=False, timeout=35) as client:
        response = await client.get("/api/derivatives/backfill", params={
            "instrument": instrument, "kind": DerivativeKind.FUNDING_RATE.value,
            "start": max(0, start), "end": end, "refresh": "true",
        })
    response.raise_for_status()
    if response.headers.get(DATA_DIR_HEADER) != directory_id:
        raise TransportError("衍生品重同步响应的数据目录与订阅不一致")
    try:
        return int(response.headers.get(NOW_MS_HEADER, ""))
    except ValueError as exc:
        raise TransportError("衍生品重同步响应缺少服务时刻") from exc


async def _subscribe(
    instrument: str, timeframe: str, *, data_dir: str | Path | None, kind: DerivativeKind | None = None,
    on_connection: Callable[[bool], None] | None = None,
    after: SubscriptionCursor | None = None, resume_from: int | None = None,
    on_cursor: Callable[[SubscriptionCursor, int], None] | None = None,
) -> AsyncGenerator[StrategyBarEvent | StrategyDerivativeEvent]:
    if timeframe not in SUPPORTED_TIMEFRAMES:
        raise InvalidRequestError(f"不支持的周期：{timeframe}")
    root = default_data_dir(data_dir)
    request = SubscriptionRequest(
        instrument=instrument, timeframe=timeframe, owner=uuid4().hex, include_baseline=True, kind=kind, report_replay_through=on_cursor is not None,
    )
    event_model = StrategyBarEvent if kind is None else StrategyDerivativeEvent
    directory_id = data_dir_id(root)
    interval = timeframe_ms(timeframe)
    cursor = after
    if cursor is None and resume_from is not None:
        cursor = SubscriptionCursor(epoch="paper-recovery", seq=0)
    coverage_start = resume_from
    seen: set[str] = set()
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
                    **({"ping_interval": 5, "ping_timeout": 5} if on_connection else {}),
                ) as socket:
                    await socket.send(request.model_copy(update={"after": cursor}).model_dump_json(
                        exclude=({"kind"} if kind is None else set()) | ({"report_replay_through"} if on_cursor is None else set()),
                    ))
                    answer = json.loads(await asyncio.wait_for(socket.recv(), timeout=10))
                    if answer.get("type") == "error":
                        raise SubscriptionRejectedError(answer["code"], answer["message"])
                    ready = SubscriptionReady.model_validate(answer)
                    if cursor is None:
                        baseline = set()
                        while len(baseline) < ready.baseline_count:
                            chunk = json.loads(await socket.recv())
                            if chunk.get("type") != "baseline" or not chunk.get("ids"):
                                raise TransportError("订阅内容基线不完整")
                            baseline.update(chunk["ids"])
                        seen.update(baseline)
                    if coverage_start is None:
                        coverage_start = ready.started_ts // interval * interval
                    if ready.type == "resync":
                        try:
                            if kind is None:
                                revisions = await _range_revisions(
                                    endpoint.base_url, directory_id, request, coverage_start=coverage_start,
                                    now=ready.server_ts, interval=interval, seen=seen,
                                    range_start=resume_from if resume_from is not None else 0,
                                )
                                recovered = [
                                    StrategyBarEvent(
                                        instrument_id=instrument, timeframe=timeframe,
                                        epoch=ready.epoch, seq=None, bar=bar, content_id=identity,
                                    )
                                    for identity, bar in revisions
                                ]
                            else:
                                as_of = ready.server_ts
                                if kind is DerivativeKind.FUNDING_RATE:
                                    as_of = await _refresh_settlements(
                                        endpoint.base_url, directory_id, instrument, coverage_start, as_of,
                                    )
                                recovered = await asyncio.to_thread(
                                    _recover_derivatives, root, instrument, kind, ready, seen, as_of=as_of,
                                )
                            for event in recovered:
                                seen.add(event.content_id)
                                yield event
                        except (HTTPStatusError, TransportError):
                            await socket.close(code=1012)
                            raise
                    cursor = SubscriptionCursor(epoch=ready.epoch, seq=ready.seq)
                    delay = 0.1
                    replay_through = ready.replay_through or ready.seq
                    def caught_up():
                        if on_cursor is not None:
                            on_cursor(cursor, coverage_start)
                        if on_connection is not None:
                            on_connection(True)
                    if cursor.seq >= replay_through:
                        caught_up()
                    while True:
                        event = event_model.model_validate_json(await socket.recv())
                        if event.epoch != cursor.epoch or event.seq is None or event.seq > cursor.seq + 1:
                            await socket.close(code=1012)
                            break
                        if event.seq <= cursor.seq:
                            continue
                        cursor = SubscriptionCursor(epoch=event.epoch, seq=event.seq)
                        identity = event.content_id
                        if identity not in seen:
                            seen.add(identity)
                            yield event
                        if cursor.seq == replay_through:
                            caught_up()
            except ConnectionClosed as exc:
                if exc.rcvd is not None and exc.rcvd.code == 1013:
                    delay = min(delay, 0.05)
            except (TimeoutError, OSError, TransportError):
                pass
            except HTTPStatusError as exc:
                if exc.response.status_code < 500:
                    raise
            except InvalidStatus as exc:
                if exc.response.status_code not in (401, 403, 502, 503, 504):
                    raise
        if on_connection is not None:
            on_connection(False)
        await asyncio.sleep(delay)
        delay = min(delay * 2, 5)
