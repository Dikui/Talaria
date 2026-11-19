#!/usr/bin/env python3
"""M2 验收用的录制回放式 DSH 宿主替身。

它只做三件事：

1. 像真实 ``dsh`` 一样启动、打印可探测的本地 URL，并读取
   ``TALARIA_DSH_BRIDGE`` 指向的桥接描述符；
2. 收到 ``/talaria/consult`` / ``/talaria/briefing`` 后，按
   ``TALARIA_M2_RECORDING``（JSONL）里的录制消息调用 Talaria 桥接端点；
3. 把每一步的真实 HTTP 结果写进 ``TALARIA_M2_LOG``，供验收测试断言。

宿主不联网、不调用模型，所有消息都是任务单 20 要求的“录制的桥接消息”。
"""

from __future__ import annotations

import json
import os
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

_LOCK = threading.Lock()


def _load_bridge() -> tuple[str, str]:
    descriptor = Path(os.environ["TALARIA_DSH_BRIDGE"])
    payload = json.loads(descriptor.read_text(encoding="utf-8"))
    return str(payload["base_url"]).rstrip("/"), str(payload["token"])


def _load_recording() -> dict[str, dict[str, Any]]:
    path = Path(os.environ["TALARIA_M2_RECORDING"])
    turns: dict[str, dict[str, Any]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        turn = json.loads(line)
        turns[str(turn["kind"])] = turn
    return turns


def _substitute(value: Any, context: dict[str, Any]) -> Any:
    if isinstance(value, str):
        # 整串就是一个占位符时保留原始类型（时间戳仍是 int）。
        for key in sorted(context, key=len, reverse=True):
            if value == f"${key}":
                return context[key]
        rendered = value
        # 长键优先，避免 ``$entry_low`` 被 ``$entry`` 之类的短键误替换。
        for key in sorted(context, key=len, reverse=True):
            rendered = rendered.replace(f"${key}", str(context[key]))
        return rendered
    if isinstance(value, list):
        return [_substitute(item, context) for item in value]
    if isinstance(value, dict):
        return {key: _substitute(item, context) for key, item in value.items()}
    return value


def _decimal(value: Any) -> Decimal:
    return Decimal(str(value))


class BridgeClient:
    """以 bridge.js 相同的方式访问 Talaria 守护进程。"""

    def __init__(self, base_url: str, token: str, log_path: Path) -> None:
        self.base_url = base_url
        self.token = token
        self.log_path = log_path

    def request(
        self, method: str, path: str, body: dict[str, Any] | None = None
    ) -> tuple[int, dict[str, Any]]:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:  # noqa: S310
                raw = response.read()
                status = response.status
        except urllib.error.HTTPError as exc:  # 4xx 也是可断言的业务结果
            raw = exc.read()
            status = exc.code
        payload = json.loads(raw) if raw else {}
        if not isinstance(payload, dict):
            payload = {"value": payload}
        return status, payload

    def record(self, entry: dict[str, Any]) -> None:
        with _LOCK:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
                handle.flush()

    def replay(self, channel: str, call: dict[str, Any], context: dict[str, Any]) -> None:
        tool = str(call["tool"])
        args = _substitute(call.get("args", {}), context)
        if tool == "get_indicators":
            status, payload = self._get_indicators(args)
        elif tool == "get_key_levels":
            status, payload = self._get_key_levels(args)
        elif tool == "get_chart_view":
            status, payload = self._get_chart_view(args)
        elif tool == "compute_operator":
            status, payload = self._compute_operator(args)
        elif tool == "write_agent_overlay":
            status, payload = self._write_agent_overlay(args)
        elif tool == "create_alert":
            status, payload = self._create_alert(args)
        elif tool == "report_usage":
            status, payload = self.request(
                "POST", "/api/collaboration/usage", args
            )
        else:  # pragma: no cover - 录制文件由测试维护
            raise AssertionError(f"未知录制工具：{tool}")
        self.record(
            {
                "channel": channel,
                "tool": tool,
                "status": status,
                "code": payload.get("code"),
                "body": payload,
            }
        )

    def _get(self, path: str) -> dict[str, Any]:
        status, payload = self.request("GET", path)
        if status != 200:
            raise AssertionError(f"GET {path} 失败：{status} {payload}")
        return payload

    def _bound_snapshot(self, snapshot_id: str) -> dict[str, Any]:
        meta = self._get(f"/api/snapshots/{snapshot_id}")
        if meta.get("snapshot_id") != snapshot_id:
            raise AssertionError(f"快照绑定不一致：{meta.get('snapshot_id')} != {snapshot_id}")
        return meta

    def _get_indicators(self, args: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        snapshot_id = str(args["snapshot_id"])
        self._bound_snapshot(snapshot_id)
        query = urllib.parse.urlencode(
            {
                "timeframe": args["timeframe"],
                "names": ",".join(args["names"]),
                "limit": args.get("limit", 300),
            }
        )
        return self.request("GET", f"/api/snapshots/{snapshot_id}/indicators?{query}")

    def _get_key_levels(self, args: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        snapshot_id = str(args["snapshot_id"])
        self._bound_snapshot(snapshot_id)
        query = urllib.parse.urlencode(
            {
                "timeframe": args["timeframe"],
                "limit": args.get("limit", 20),
                "order": "strength_desc",
                "evidence_class": "ALL",
                "vwap_anchor": "session",
                "value_area_ratio": 0.7,
            }
        )
        return self.request("GET", f"/api/snapshots/{snapshot_id}/key-levels?{query}")

    def _get_chart_view(self, args: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        snapshot_id = str(args["snapshot_id"])
        self._bound_snapshot(snapshot_id)
        query = urllib.parse.urlencode(
            {
                "instrument_id": args["instrument_id"],
                "timeframe": args["timeframe"],
                "from_ts": args["from_ts"],
                "to_ts": args["to_ts"],
                "price_min": args["price_min"],
                "price_max": args["price_max"],
            }
        )
        return self.request("GET", f"/api/chart-view?{query}")

    def _compute_operator(self, args: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        snapshot_id = str(args["snapshot_id"])
        self._bound_snapshot(snapshot_id)
        body = {
            "snapshot_id": snapshot_id,
            "timeframe": args["timeframe"],
            "operator": args["operator"],
            "params": args.get("params", {}),
            "limit": args.get("limit", 300),
        }
        return self.request("POST", "/api/operators/compute", body)

    def _write_agent_overlay(
        self, args: dict[str, Any]
    ) -> tuple[int, dict[str, Any]]:
        return self.request("POST", "/api/annotations", args)

    def _create_alert(self, args: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        snapshot_id = str(args["snapshot_id"])
        self._bound_snapshot(snapshot_id)
        body = {
            "instrument_id": args["instrument_id"],
            "timeframe": args["timeframe"],
            "condition": args["condition"],
            "note": args.get("note", ""),
            "repeat": args.get("repeat", True),
            "cooldown_ms": args.get("cooldown_ms", 60000),
            "expires_at": args.get("expires_at"),
            "draft": True,
        }
        return self.request("POST", "/api/alerts", body)


class ReplayHost:
    def __init__(self, bridge: BridgeClient, turns: dict[str, dict[str, Any]]) -> None:
        self.bridge = bridge
        self.turns = turns

    def _context(
        self,
        *,
        snapshot_id: str,
        consultation_id: str | None,
        package: dict[str, Any] | None,
    ) -> dict[str, Any]:
        context: dict[str, Any] = {
            "snapshot_id": snapshot_id,
            "instrument": INSTRUMENT,
            "timeframe": TIMEFRAME,
            "session_id": SESSION_ID,
        }
        if consultation_id is not None:
            context["consultation_id"] = consultation_id
            context["chartview_id"] = f"chartview:{consultation_id}:{TIMEFRAME}"
        page = self.bridge._get(
            f"/api/snapshots/{snapshot_id}/bars?timeframe={TIMEFRAME}&limit=300"
        )
        bars = [bar for bar in page["bars"] if bar.get("close") is not None]
        if not bars:
            raise AssertionError("快照没有可用于锚定的 K 线")
        last = bars[-1]
        close = _decimal(last["close"])
        context["last_bar_ts"] = int(last["timestamp"])
        context["last_close"] = format(close, "f")
        context["entry_low"] = format(close - Decimal("80"), "f")
        context["entry_high"] = format(close - Decimal("20"), "f")
        context["stop"] = format(close - Decimal("300"), "f")
        context["take_profit"] = format(close + Decimal("500"), "f")
        if package is not None:
            visible = package["visible"]
            context["visible_from_ts"] = int(visible["from_ts"])
            context["visible_to_ts"] = int(visible["to_ts"])
            context["visible_price_min"] = str(visible["price_min"])
            context["visible_price_max"] = str(visible["price_max"])
        return context

    def handle_consult(self, payload: dict[str, Any]) -> None:
        consultation_id = str(payload["consultation_id"])
        status, record = self.bridge.request(
            "GET", f"/api/collaboration/consultations/{consultation_id}"
        )
        if status != 200:
            self.bridge.record(
                {"channel": "consult", "tool": "__load__", "status": status, "body": record}
            )
            return
        turn = self.turns["consult"]
        context = self._context(
            snapshot_id=str(record["snapshot_id"]),
            consultation_id=consultation_id,
            package=record.get("package"),
        )
        for call in turn.get("calls", []):
            self.bridge.replay("consult", call, context)
        result = {
            "text": _substitute(turn["result_text"], context),
            "dsh_session_id": SESSION_ID,
            "dsh_turn_id": str(turn["turn_id"]),
        }
        status, body = self.bridge.request(
            "POST", f"/api/collaboration/consultations/{consultation_id}/result", result
        )
        self.bridge.record(
            {
                "channel": "consult",
                "tool": "__result__",
                "status": status,
                "code": body.get("code"),
                "body": body,
            }
        )
        # 同一 turn 的用量上报两次：第一次落库，第二次必须被判重。
        for _ in range(2):
            self.bridge.replay(
                "consult", {"tool": "report_usage", "args": turn["usage"]}, context
            )

    def handle_briefing(self, payload: dict[str, Any]) -> None:
        briefing_id = str(payload["briefing_id"])
        snapshot_id = str(payload["snapshot_id"])
        turn = self.turns["briefing"]
        context = self._context(
            snapshot_id=snapshot_id, consultation_id=None, package=None
        )
        context["briefing_id"] = briefing_id
        context["trigger_id"] = str(payload.get("trigger_id", ""))
        for call in turn.get("calls", []):
            self.bridge.replay("briefing", call, context)
        result = {
            "content": _substitute(turn["content"], context),
            "dsh_session_id": SESSION_ID,
            "dsh_turn_id": str(turn["turn_id"]),
            "usage": turn["usage"],
        }
        status, body = self.bridge.request(
            "POST", f"/api/scanner/briefings/{briefing_id}/result", result
        )
        self.bridge.record(
            {
                "channel": "briefing",
                "tool": "__result__",
                "status": status,
                "code": body.get("code"),
                "body": body,
            }
        )


INSTRUMENT = "BTC-USDT-PERP"
TIMEFRAME = "1m"
SESSION_ID = "m2-recorded-session"


class Handler(BaseHTTPRequestHandler):
    server_version = "M2ReplayHost/1.0"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        return

    def _write(self, status: int, payload: dict[str, Any] | None = None) -> None:
        body = json.dumps(payload or {}, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = urllib.parse.urlsplit(self.path).path
        if path == "/":
            self._write(200, {"ok": True, "host": "m2-replay"})
            return
        self._write(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        path = urllib.parse.urlsplit(self.path).path
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        payload = json.loads(raw or b"{}")
        replay: ReplayHost = self.server.replay  # type: ignore[attr-defined]
        try:
            if path == "/talaria/consult":
                replay.handle_consult(payload)
            elif path == "/talaria/briefing":
                replay.handle_briefing(payload)
            elif path == "/talaria/bootstrap":
                pass
            else:
                self._write(404, {"error": "not found"})
                return
        except Exception as exc:  # 让 daemon 侧看到宿主失败而不是静默挂起
            replay.bridge.record(
                {
                    "channel": "host",
                    "tool": "__error__",
                    "status": 500,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            self._write(500, {"error": str(exc)})
            return
        self._write(200, {"ok": True})


def _parse_port(argv: list[str]) -> int:
    for index, item in enumerate(argv):
        if item == "--port" and index + 1 < len(argv):
            return int(argv[index + 1])
    return 18931


def main(argv: list[str]) -> int:
    bridge_url, token = _load_bridge()
    turns = _load_recording()
    log_path = Path(os.environ["TALARIA_M2_LOG"])
    bridge = BridgeClient(bridge_url, token, log_path)
    replay = ReplayHost(bridge, turns)
    port = _parse_port(argv)
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.replay = replay  # type: ignore[attr-defined]
    print(f"http://127.0.0.1:{port}/?token=recorded", flush=True)
    try:
        server.serve_forever(poll_interval=0.05)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
