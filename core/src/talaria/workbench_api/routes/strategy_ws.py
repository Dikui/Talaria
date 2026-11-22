"""收盘柱和衍生品修订的可靠实时通道。"""

import asyncio
import secrets
from contextlib import suppress

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from talaria.foundation.errors import TalariaError
from talaria.foundation.strategy_subscription import SubscriptionRequest
from talaria.foundation.timeframes import SUPPORTED_TIMEFRAMES
from talaria.packs import get_market_pack
from talaria.service_endpoint import DATA_DIR_HEADER, data_dir_id
from talaria.workbench_api.routes import get_runtime
from talaria.workbench_api.strategy_stream import StrategySession

router = APIRouter()


async def _send(websocket: WebSocket, session: StrategySession) -> None:
    await websocket.send_text(session.ready.model_dump_json())
    for offset in range(0, len(session.baseline), 512):
        await websocket.send_json({"type": "baseline", "ids": session.baseline[offset:offset + 512]})
    seq = session.ready.seq
    for event in session.replay:
        await websocket.send_text(event.model_dump_json())
        assert event.seq is not None
        seq = event.seq
    while True:
        event = await session.queue.get()
        assert event.seq is not None
        if event.seq > seq:
            await websocket.send_text(event.model_dump_json())
            seq = event.seq


async def _receive_release(websocket: WebSocket) -> bool:
    # 单通道连接；正常 WS 关闭释放配额，异常断线保留短暂的重连租约。
    while True:
        message = await websocket.receive()
        if message["type"] == "websocket.disconnect":
            return message["code"] in (1000, 1001)


async def _reject(websocket: WebSocket, code: str, message: str) -> None:
    await websocket.send_json({"type": "error", "code": code, "message": message})
    await websocket.close(code=1008)


@router.websocket("/ws/strategy")
async def strategy_socket(websocket: WebSocket) -> None:
    scheme, _, token = websocket.headers.get("authorization", "").partition(" ")
    directory = websocket.headers.get(DATA_DIR_HEADER)
    if (
        scheme.lower() != "bearer" or not secrets.compare_digest(token.encode(), websocket.app.state.token.encode())
        or (directory is not None and directory != data_dir_id(websocket.app.state.settings.data_dir))
    ):
        await websocket.close(code=1008)
        return
    await websocket.accept()
    runtime = get_runtime(websocket)
    stream = runtime.strategy_stream
    if stream is None:
        await _reject(websocket, "FEED_UNAVAILABLE", "常驻服务未启用实时行情")
        return
    try:
        request = SubscriptionRequest.model_validate_json(
            await asyncio.wait_for(websocket.receive_text(), timeout=5),
        )
    except WebSocketDisconnect:
        return
    except (TimeoutError, ValidationError, KeyError):
        await _reject(websocket, "VALIDATION_ERROR", "无效的策略订阅请求")
        return
    if request.instrument not in {spec.instrument_id for spec in runtime.workbench.instruments()}:
        await _reject(websocket, "INSTRUMENT_NOT_FOUND", f"未知标的：{request.instrument}")
        return
    if request.timeframe not in SUPPORTED_TIMEFRAMES:
        await _reject(websocket, "VALIDATION_ERROR", f"不支持的周期：{request.timeframe}")
        return
    key = (request.instrument, request.timeframe)
    if request.kind is not None:
        capability = get_market_pack(websocket.app.state.settings.pack).traits(request.instrument).derivatives
        if (
            runtime.cold_publisher is None or capability is None
            or not capability.realtime or request.kind not in capability.kinds
        ):
            await _reject(websocket, "FEED_UNAVAILABLE", "该标的的衍生品实时冷库不可用")
            return
        key = (*key, request.kind)
    try:
        session = await stream.attach(
            request.owner, key, request.after,
            include_baseline=request.include_baseline,
        )
    except TalariaError as exc:
        await _reject(websocket, exc.code, exc.message)
        return
    except ValueError as exc:
        await _reject(websocket, "VALIDATION_ERROR", str(exc))
        return

    sender = asyncio.create_task(_send(websocket, session))
    receiver = asyncio.create_task(_receive_release(websocket))
    overloaded = asyncio.create_task(session.interrupted.wait())
    tasks = (sender, receiver, overloaded)
    release = False
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        release = receiver in done and receiver.result()
    except asyncio.CancelledError:
        return
    finally:
        for task in tasks:
            task.cancel()
        stream.detach(request.owner, session, release=release)
        with suppress(asyncio.CancelledError):
            await asyncio.gather(*tasks, return_exceptions=True)
    if overloaded in done or (sender in done and receiver not in done):
        with suppress(WebSocketDisconnect, RuntimeError, OSError):
            await websocket.close(code=1013, reason="请用最后的变更序号重连")
