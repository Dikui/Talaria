"""工作台实时 WS 订阅与投影。"""

from __future__ import annotations

import asyncio
import contextlib

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from talaria.foundation.errors import TalariaError
from talaria.foundation.feed import (
    DerivativeTickEvent,
    FeedEvent,
    FeedHealthEvent,
    KlineEvent,
    OrderBookEvent,
    TradeEvent,
)
from talaria.foundation.timeframes import SUPPORTED_TIMEFRAMES
from talaria.workbench_api.desk_broadcast import enqueue, levels_payload
from talaria.workbench_api.routes import get_runtime

router = APIRouter()


def _ws_payload(event: FeedEvent) -> dict[str, object]:
    if isinstance(event, KlineEvent):
        return {"type": "bar", "data": event.model_dump(mode="json")}
    if isinstance(event, OrderBookEvent):
        return {"type": "book", "data": event.model_dump(mode="json")}
    if isinstance(event, DerivativeTickEvent):
        return {"type": "derivatives", "data": event.model_dump(mode="json")}
    return {"type": "health", "data": event.model_dump(mode="json")}


def _wanted(event: FeedEvent, subscriptions: set[tuple[str, str]]) -> bool:
    if isinstance(event, KlineEvent):
        return (event.instrument_id, event.timeframe) in subscriptions
    if isinstance(event, FeedHealthEvent):
        return (event.instrument_id, event.timeframe) in subscriptions
    return any(
        instrument_id == event.instrument_id for instrument_id, _ in subscriptions
    )


async def _send_loop(websocket: WebSocket, queue: asyncio.Queue[dict[str, object]]) -> None:
    try:
        while True:
            await websocket.send_json(await queue.get())
    except Exception:  # 客户端断开或运行时已关闭，直接结束发送循环
        return


@router.websocket("/ws/desk")
async def desk_socket(websocket: WebSocket) -> None:
    await websocket.accept()
    runtime = get_runtime(websocket)
    supervisor = runtime.supervisor
    scheduler = runtime.subscription_scheduler
    subscriptions: set[tuple[str, str]] = set()
    denied = scheduler.denied_alerts if scheduler is not None else []
    await websocket.send_json(
        {
            "type": "hello",
            "denied_alerts": denied,
            "feed_mode": runtime.workbench.feed_mode,
            "server_ts": websocket.app.state.settings.now_ms(),
            "live": supervisor is not None,
        }
    )
    queue = runtime.desk.attach(subscriptions)
    if scheduler is not None and scheduler.denied_alerts != denied:
        enqueue(queue, {"type": "alert_quota", "denied_alerts": scheduler.denied_alerts})
    watchlist = runtime.watchlist
    valid_ids = {spec.instrument_id for spec in runtime.workbench.instruments()}
    retainers = runtime.desk.chart_subscribers

    async def rebalance_watchlist() -> None:
        with contextlib.suppress(TalariaError):
            await watchlist.rebalance()

    def listener(event: FeedEvent) -> None:
        if isinstance(event, TradeEvent) or not _wanted(event, subscriptions):
            return
        enqueue(queue, _ws_payload(event))

    def retain(key: tuple[str, str]) -> None:
        retainers[key] = retainers.get(key, 0) + 1

    def release(key: tuple[str, str]) -> None:
        remaining = retainers.get(key, 0) - 1
        if remaining > 0:
            retainers[key] = remaining
        else:
            retainers.pop(key, None)

    remove_listener = (
        supervisor.add_listener(listener) if supervisor is not None else None
    )
    sender = asyncio.create_task(_send_loop(websocket, queue))
    try:
        while True:
            message = await websocket.receive_json()
            if not isinstance(message, dict):
                continue
            op = message.get("op")
            instrument_id = str(message.get("instrument_id") or "")
            timeframe = str(message.get("timeframe") or "")
            key = (instrument_id, timeframe)
            if op == "subscribe":
                if key in subscriptions:
                    continue
                if instrument_id not in valid_ids or timeframe not in SUPPORTED_TIMEFRAMES:
                    code, message = (
                        ("INSTRUMENT_NOT_FOUND", f"未知标的：{instrument_id}")
                        if instrument_id not in valid_ids
                        else ("VALIDATION_ERROR", f"不支持的周期：{timeframe}")
                    )
                    await websocket.send_json({"type": "error", "code": code, "message": message})
                    continue
                had_channel = supervisor is not None and supervisor.has_channel(
                    instrument_id, timeframe
                )
                subscriptions.add(key)
                if scheduler is not None:
                    try:
                        scheduler.acquire_chart(key)
                    except TalariaError as exc:
                        subscriptions.discard(key)
                        await websocket.send_json(
                            {
                                "type": "error",
                                "code": exc.code,
                                "owner": "chart",
                                "instrument_id": instrument_id,
                                "timeframe": timeframe,
                                "message": exc.message,
                            }
                        )
                        continue
                if supervisor is not None:
                    retain(key)
                    await rebalance_watchlist()
                    if had_channel:
                        current = supervisor.channel_health(
                            instrument_id, timeframe
                        )
                        if current is not None:
                            enqueue(queue, _ws_payload(current))
                latest = runtime.key_levels.latest(key)
                if latest is not None:
                    enqueue(queue, levels_payload(latest))
            elif op == "unsubscribe":
                if key not in subscriptions:
                    continue
                subscriptions.discard(key)
                if supervisor is not None:
                    release(key)
                if scheduler is not None:
                    scheduler.release_chart(key)
                if supervisor is not None and retainers.get(key, 0) == 0:
                    await rebalance_watchlist()
            else:
                await websocket.send_json(
                    {
                        "type": "error",
                        "code": "VALIDATION_ERROR",
                        "message": "未知的订阅操作",
                    }
                )
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        runtime.desk.detach(queue)
        if remove_listener is not None:
            remove_listener()
        sender.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await sender
        for key in subscriptions:
            if supervisor is not None:
                release(key)
            if scheduler is not None:
                scheduler.release_chart(key)
        if supervisor is not None:
            await rebalance_watchlist()
