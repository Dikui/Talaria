"""Chain 3 端到端验收与崩溃恢复测试（Ticket 10）。

严格只启动编译后的 Rust `talaria-data`（承载 `/desk` SPA 与 `/api/paper/*`）与
真实 Python CLI 子进程 `talaria account run <scheme> --mode paper`，全程不启动、
不导入 Python `workbench_api`，并用真实无头 Chromium（CDP）在 1440x900 分辨率下
驱动完整闭环与深浅双主题截图：

1. 启动账户进程 -> 图表下单手势（`'position'` + Rust 定仓预演，零 `HUMAN_DRAWING` 污染）
   -> 生成草案 -> 保存并确认草案；
2. 强杀并重启 `talaria account`（确认后/回执窗口）-> 断言无丢草案、无重复执行；
3. 模拟行情推进成交 -> 图表出现 `EXECUTION_BARRIER` 与成交标记，`AccountPanel` 显示持仓；
4. 在图表上真实拖拽止损线收紧止损 -> 二次确认弹窗提交 `TIGHTEN_STOP` -> 投影更新；
5. 持仓期间强杀 `talaria account` -> 5 秒内检测离线 -> 重启恢复，无重复成交；
6. 在 `AccountPanel` 提交 50% 减仓命令后强杀并重启 `talaria-data` -> 验证 SQLite 命令队列
   不丢命令，账户进程重连新端口完成 50% 减仓；
7. 在 `AccountPanel` 点击全平（`CLOSE_ALL`）-> 回合平仓进入历史与成交列表；
8. 最终校验：`accounts/<id>/view.json` 与权威账本 `ledger.sqlite` 重放一致、每笔账本分录
   按币种双边平账（sum == 0）、无重复成交、无丢草案、无丢命令、`workbench_api` 未加载。
"""

from __future__ import annotations

import asyncio
import base64
from collections import defaultdict
from decimal import Decimal
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import time
from typing import Any

import httpx
import pytest
import websockets

from talaria.account.ledger import read_journal
from talaria.account.paper_projection import projection_path
from talaria.account.plugins.risk_math import collateral_balance
from talaria.foundation.paper import PaperAccountView
from talaria.service_endpoint import ServiceConnection, read_service_connection

pytestmark = [pytest.mark.large, pytest.mark.spec]

INSTRUMENT = "BTC-USDT-PERP"
SUB_ACCOUNT = "paper-main"
REPO_ROOT = Path(__file__).resolve().parents[2]
ENGINE_DIR = REPO_ROOT / "engine"
DESK_DIST = REPO_ROOT / "workbench/packages/talaria-desk-ui/dist"
FIXTURE_DIR = REPO_ROOT / "tests/fixtures/bybit"

IDLE_STRATEGY_SOURCE = """\
from talaria.account.scheme import INTERFACE_VERSION, Slot, SlotPlugin
from talaria.components import ComponentMeta
from talaria.foundation.collaboration.contracts import Abstain


class ManualOnlyStrategy:
    meta = ComponentMeta(
        name="manual_only",
        version="1",
        inputs=("bars",),
        output_type="strategy_decision",
        parameters_schema={},
    )

    def __init__(self, context, params):
        self.context = context

    def on_bar(self, ctx):
        return Abstain(reason_code="EVIDENCE_CONFLICT", message="manual-only")


plugin = SlotPlugin(
    Slot.STRATEGY,
    (INTERFACE_VERSION,),
    ManualOnlyStrategy,
    ManualOnlyStrategy.meta.parameters_schema,
)
"""


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _talaria_data_binary() -> Path:
    for candidate in (
        ENGINE_DIR / "target/debug/talaria-data",
        ENGINE_DIR / "target/release/talaria-data",
    ):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    if shutil.which("cargo"):
        subprocess.run(
            ["cargo", "build", "--bin", "talaria-data"],
            cwd=ENGINE_DIR,
            check=True,
        )
        return ENGINE_DIR / "target/debug/talaria-data"
    pytest.skip("talaria-data binary unavailable")


def _find_chromium_binary() -> Path:
    env_bin = os.environ.get("TALARIA_CHROME_BIN")
    if env_bin and Path(env_bin).is_file():
        return Path(env_bin)
    cache_root = Path.home() / "Library/Caches/ms-playwright"
    if cache_root.is_dir():
        for pattern in (
            "chromium_headless_shell-*/chrome-headless-shell-mac-arm64/chrome-headless-shell",
            "chromium_headless_shell-*/chrome-headless-shell-mac-x64/chrome-headless-shell",
            "chromium-*/chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
            "chromium-*/chrome-mac/Chromium.app/Contents/MacOS/Chromium",
        ):
            matches = sorted(cache_root.glob(pattern), reverse=True)
            for match in matches:
                if match.is_file() and os.access(match, os.X_OK):
                    return match
    for fallback in (
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
    ):
        if Path(fallback).is_file():
            return Path(fallback)
    pytest.skip("Headless Chromium binary unavailable")


def _stop_proc(proc: subprocess.Popen[str] | None, *, force: bool = False) -> None:
    if proc is None or proc.poll() is not None:
        return
    if force:
        proc.kill()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        return
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=8)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def _read_first_minute(data_dir: Path) -> int:
    import sqlite3

    ledger_path = data_dir / "accounts" / SUB_ACCOUNT / "ledger.sqlite"
    with sqlite3.connect(ledger_path) as con:
        rows = con.execute("SELECT * FROM paper_checkpoint").fetchall()
    assert rows, "Missing paper_checkpoint row in ledger.sqlite"
    payload = json.loads(rows[-1][-1])
    first_minute = payload.get("feed", {}).get("first_minute")
    assert isinstance(first_minute, int), f"Missing feed.first_minute in checkpoint: {payload.get('feed')}"
    return first_minute


def _write_replay_script(
    path: Path,
    bars: list[tuple[int, int, str, str, str, str, bool, int]],
    *,
    marks: list[tuple[int, int, str]] = (),
) -> None:
    """Write deterministic JSONL replay script.

    Each bar tuple: (offset_ms, timestamp_ms, open, high, low, close, confirmed, revision).
    Each mark tuple: (offset_ms, ts_event_ms, mark_price_str).
    Confirmed 1m bars respect strict point-in-time causality (`ts_available = timestamp + 60_000`).
    """
    items: list[tuple[int, dict[str, Any]]] = [(0, {"offset_ms": 0, "type": "heartbeat"})]
    for offset_ms, ts, o, h, l, c, confirmed, rev in bars:
        items.append(
            (
                offset_ms,
                {
                    "offset_ms": offset_ms,
                    "type": "kline",
                    "event": {
                        "instrument_id": INSTRUMENT,
                        "timeframe": "1m",
                        "bar": {
                            "timestamp": ts,
                            "open": o,
                            "high": h,
                            "low": l,
                            "close": c,
                            "volume": "12",
                            "turnover": str(Decimal(c) * Decimal("12")),
                            "status": "NORMAL",
                            "ts_available": ts + 60_000 if confirmed else ts,
                            "precision": "SECOND_VERIFIED",
                        },
                        "confirmed": confirmed,
                        "revision": rev,
                    },
                },
            )
        )
    for offset_ms, ts, price in marks:
        items.append(
            (
                offset_ms,
                {
                    "offset_ms": offset_ms,
                    "type": "derivative",
                    "event": {
                        "instrument_id": INSTRUMENT,
                        "mark_price": price,
                        "ts_event": ts,
                        "ts_ingested": ts,
                    },
                },
            )
        )
    items.sort(key=lambda pair: pair[0])
    path.write_text("\n".join(json.dumps(item) for _, item in items) + "\n", encoding="utf-8")


def _start_talaria_data(
    data_dir: Path,
    replay_file: Path,
    *,
    prev_port: int | None = None,
) -> tuple[subprocess.Popen[str], ServiceConnection]:
    assert DESK_DIST.is_dir() and (DESK_DIST / "index.html").is_file(), (
        f"Missing built desk SPA at {DESK_DIST}; run `pnpm -C workbench build` first."
    )
    port = _free_port()
    while port == prev_port:
        port = _free_port()
    proc = subprocess.Popen(
        [
            str(_talaria_data_binary()),
            "replay",
            "--data-dir",
            str(data_dir),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--pack",
            "crypto_perp",
            "--desk-dist",
            str(DESK_DIST),
            "--fixture-dir",
            str(FIXTURE_DIR),
            "--replay-file",
            str(replay_file),
            "--replay-speed",
            "1.0",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + 15.0
    expected_url = f"http://127.0.0.1:{port}"
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            out, err = proc.communicate(timeout=2)
            raise RuntimeError(f"talaria-data exited early ({proc.returncode}):\n{out}\n{err}")
        conn = read_service_connection(data_dir)
        if conn is not None and conn.base_url == expected_url:
            try:
                resp = httpx.get(
                    f"{conn.base_url}/api/health",
                    headers={"Authorization": f"Bearer {conn.token}"},
                    timeout=1.0,
                )
                if resp.status_code == 200:
                    return proc, conn
            except httpx.HTTPError:
                pass
        time.sleep(0.08)
    _stop_proc(proc, force=True)
    raise TimeoutError("Timed out waiting for talaria-data")


def _start_account_process(
    scheme_path: Path,
    data_dir: Path,
    log_path: Path,
) -> subprocess.Popen[str]:
    log_file = log_path.open("a", encoding="utf-8")
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "talaria",
            "account",
            "run",
            str(scheme_path),
            "--mode",
            "paper",
            "--data-dir",
            str(data_dir),
        ],
        stdout=log_file,
        stderr=subprocess.STDOUT,
        text=True,
        env=env,
    )


def _api_headers(conn: ServiceConnection) -> dict[str, str]:
    return {"Authorization": f"Bearer {conn.token}"}


def _wait_for_projection(
    data_dir: Path,
    predicate,
    *,
    timeout: float = 15.0,
    description: str = "projection condition",
    proc: subprocess.Popen[str] | None = None,
    log_path: Path | None = None,
) -> PaperAccountView:
    path = projection_path(data_dir, SUB_ACCOUNT)
    deadline = time.monotonic() + timeout
    last_view: PaperAccountView | None = None
    while time.monotonic() < deadline:
        if proc is not None and proc.poll() is not None:
            logs = log_path.read_text(encoding="utf-8") if log_path and log_path.is_file() else ""
            raise RuntimeError(
                f"talaria account exited early ({proc.returncode}) waiting for {description}:\n{logs}"
            )
        if path.is_file():
            try:
                view = PaperAccountView.model_validate_json(path.read_text(encoding="utf-8"))
                last_view = view
                if predicate(view):
                    return view
            except ValueError:
                pass
        time.sleep(0.1)
    logs = log_path.read_text(encoding="utf-8") if log_path and log_path.is_file() else ""
    raise TimeoutError(
        f"Timed out waiting for {description}. last_view={last_view}\naccount_logs:\n{logs[-2000:]}"
    )


class _CdpBrowser:
    """Minimal synchronous CDP client driving Chrome Headless Shell at 1440x900."""

    def __init__(self, tmp_dir: Path, width: int = 1440, height: int = 900) -> None:
        self._width = width
        self._height = height
        self._port = _free_port()
        self._user_data = tmp_dir / f"chrome-profile-{self._port}"
        self._user_data.mkdir(parents=True, exist_ok=True)
        self._proc: subprocess.Popen[str] | None = None
        self._loop = asyncio.new_event_loop()
        self._ws = None
        self._msg_id = 0
        self.page_errors: list[str] = []

    def __enter__(self) -> _CdpBrowser:
        chrome_bin = _find_chromium_binary()
        self._proc = subprocess.Popen(
            [
                str(chrome_bin),
                "--headless",
                "--disable-gpu",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                f"--window-size={self._width},{self._height}",
                f"--remote-debugging-port={self._port}",
                f"--user-data-dir={self._user_data}",
                "about:blank",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        ws_url = self._wait_for_target_ws()
        self._ws = self._loop.run_until_complete(
            websockets.connect(ws_url, max_size=32 * 1024 * 1024)
        )
        self.send("Page.enable")
        self.send("Runtime.enable")
        self.send("DOM.enable")
        self.send(
            "Emulation.setDeviceMetricsOverride",
            {
                "width": self._width,
                "height": self._height,
                "deviceScaleFactor": 1,
                "mobile": False,
            },
        )
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._ws is not None:
            try:
                self._loop.run_until_complete(self._ws.close())
            except Exception:
                pass
        self._loop.close()
        _stop_proc(self._proc, force=True)

    def _wait_for_target_ws(self) -> str:
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            try:
                resp = httpx.get(f"http://127.0.0.1:{self._port}/json", timeout=1.0)
                if resp.status_code == 200:
                    for target in resp.json():
                        if target.get("type") == "page" and target.get("webSocketDebuggerUrl"):
                            return target["webSocketDebuggerUrl"]
            except httpx.HTTPError:
                pass
            time.sleep(0.1)
        raise TimeoutError("Timed out waiting for Chrome Headless Shell CDP endpoint")

    async def _send_async(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        assert self._ws is not None
        self._msg_id += 1
        req_id = self._msg_id
        payload: dict[str, Any] = {"id": req_id, "method": method}
        if params is not None:
            payload["params"] = params
        await self._ws.send(json.dumps(payload))
        while True:
            raw = await asyncio.wait_for(self._ws.recv(), timeout=20.0)
            msg = json.loads(raw)
            if msg.get("method") == "Runtime.exceptionThrown":
                details = msg.get("params", {}).get("exceptionDetails", {})
                self.page_errors.append(str(details.get("text") or details))
            if msg.get("id") == req_id:
                if "error" in msg:
                    raise RuntimeError(f"CDP {method} failed: {msg['error']}")
                return msg.get("result", {})

    def send(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._loop.run_until_complete(self._send_async(method, params))

    def navigate(self, url: str) -> None:
        self.send("Page.navigate", {"url": url})
        self.wait_for_condition("document.readyState === 'complete'", timeout=15.0)

    def eval_js(self, expression: str) -> Any:
        result = self.send(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": True,
            },
        )
        if "exceptionDetails" in result:
            raise RuntimeError(f"JS eval error: {result['exceptionDetails']}")
        return result.get("result", {}).get("value")

    def wait_for_condition(self, js_expr: str, timeout: float = 15.0, poll: float = 0.1) -> Any:
        deadline = time.monotonic() + timeout
        last_val = None
        while time.monotonic() < deadline:
            last_val = self.eval_js(js_expr)
            if last_val:
                return last_val
            time.sleep(poll)
        raise TimeoutError(f"Timed out waiting for JS condition: {js_expr} (last={last_val!r})")

    def wait_for_selector(self, selector: str, timeout: float = 15.0) -> None:
        sel = json.dumps(selector)
        self.wait_for_condition(f"Boolean(document.querySelector({sel}))", timeout=timeout)

    def click_selector(self, selector: str) -> None:
        self.wait_for_selector(selector, timeout=15.0)
        sel = json.dumps(selector)
        box = None
        for _ in range(5):
            box = self.eval_js(
                f"""new Promise((resolve) => {{
                    const el = document.querySelector({sel});
                    if (!el) {{ resolve(null); return; }}
                    el.scrollIntoView({{ block: 'center', inline: 'center' }});
                    requestAnimationFrame(() => {{
                        const r = el.getBoundingClientRect();
                        const x = r.left + r.width / 2;
                        const y = r.top + r.height / 2;
                        const hit = document.elementFromPoint(x, y);
                        resolve({{
                            x, y, w: r.width, h: r.height,
                            disabled: Boolean(el.disabled),
                            hitMatch: Boolean(hit && (hit === el || el.contains(hit) || hit.contains(el))),
                            hitDesc: hit ? `${{hit.tagName}}.${{hit.className}}` : null,
                        }});
                    }});
                }})"""
            )
            if box is not None and box["w"] > 0 and box["h"] > 0 and not box["disabled"] and box["hitMatch"]:
                break
            time.sleep(0.15)
        assert box is not None and box["w"] > 0 and box["h"] > 0, f"Element not clickable: {selector} ({box})"
        assert not box["disabled"], f"Element is disabled: {selector} ({box})"
        self.mouse_click(box["x"], box["y"])

    def fill_input(self, selector: str, value: str) -> None:
        self.wait_for_selector(selector, timeout=15.0)
        sel = json.dumps(selector)
        val = json.dumps(value)
        ok = self.eval_js(
            f"""(() => {{
                const el = document.querySelector({sel});
                if (!el) return false;
                el.focus();
                const proto = el.tagName === 'TEXTAREA'
                    ? window.HTMLTextAreaElement.prototype
                    : el.tagName === 'SELECT'
                        ? window.HTMLSelectElement.prototype
                        : window.HTMLInputElement.prototype;
                const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
                const tracker = el._valueTracker;
                if (setter) setter.call(el, {val});
                else el.value = {val};
                if (tracker) tracker.setValue('');
                el.dispatchEvent(new Event('input', {{ bubbles: true }}));
                el.dispatchEvent(new Event('change', {{ bubbles: true }}));
                return el.value === {val};
            }})()"""
        )
        assert ok, f"Failed to fill {selector} with {value!r}"

    def mouse_move(self, x: float, y: float, *, buttons: int = 0) -> None:
        self.send(
            "Input.dispatchMouseEvent",
            {
                "type": "mouseMoved",
                "x": float(x),
                "y": float(y),
                "button": "left" if buttons else "none",
                "buttons": buttons,
            },
        )

    def mouse_click(self, x: float, y: float) -> None:
        self.mouse_move(x, y)
        self.send(
            "Input.dispatchMouseEvent",
            {"type": "mousePressed", "x": float(x), "y": float(y), "button": "left", "clickCount": 1, "buttons": 1},
        )
        self.send(
            "Input.dispatchMouseEvent",
            {"type": "mouseReleased", "x": float(x), "y": float(y), "button": "left", "clickCount": 1, "buttons": 0},
        )

    def mouse_drag(self, x1: float, y1: float, x2: float, y2: float, *, steps: int = 12) -> None:
        self.mouse_move(x1, y1)
        time.sleep(0.05)
        self.send(
            "Input.dispatchMouseEvent",
            {"type": "mousePressed", "x": float(x1), "y": float(y1), "button": "left", "clickCount": 1, "buttons": 1},
        )
        for i in range(1, steps + 1):
            frac = i / steps
            cx = x1 + (x2 - x1) * frac
            cy = y1 + (y2 - y1) * frac
            self.mouse_move(cx, cy, buttons=1)
            time.sleep(0.025)
        self.send(
            "Input.dispatchMouseEvent",
            {"type": "mouseReleased", "x": float(x2), "y": float(y2), "button": "left", "clickCount": 1, "buttons": 0},
        )

    def set_theme(self, theme: str) -> None:
        current = self.eval_js("document.documentElement.dataset.theme || 'dark'")
        if current != theme:
            self.click_selector("[data-testid='theme-toggle']")
            self.wait_for_condition(
                f"(document.documentElement.dataset.theme || 'dark') === {json.dumps(theme)}",
                timeout=5.0,
            )
            time.sleep(0.25)

    def capture_theme_pair(self, out_dir: Path, prefix: str) -> tuple[Path, Path]:
        out_dir.mkdir(parents=True, exist_ok=True)
        orig_theme = self.eval_js("document.documentElement.dataset.theme || 'dark'")
        dark_path = out_dir / f"{prefix}_dark.png"
        light_path = out_dir / f"{prefix}_light.png"
        for theme, path in (("dark", dark_path), ("light", light_path)):
            self.set_theme(theme)
            time.sleep(0.3)
            shot = self.send("Page.captureScreenshot", {"format": "png"})
            path.write_bytes(base64.b64decode(shot["data"]))
            assert path.stat().st_size > 10_000, f"Screenshot too small: {path}"
        self.set_theme(orig_theme)
        return dark_path, light_path


def _open_dock_tab(browser: _CdpBrowser, title_candidates: tuple[str, ...]) -> None:
    """Click a Dockview right-dock tab by its visible label (zh or en) using real mouse pointer events."""
    titles_js = json.dumps(list(title_candidates))
    box = browser.eval_js(
        f"""(() => {{
            const titles = {titles_js};
            const tabs = Array.from(document.querySelectorAll('.dv-tab, [role="tab"]'));
            for (const tab of tabs) {{
                const text = (tab.textContent || '').trim();
                if (titles.some((t) => text.includes(t))) {{
                    tab.scrollIntoView({{ block: 'nearest', inline: 'center' }});
                    const r = tab.getBoundingClientRect();
                    return {{ text, x: r.left + r.width / 2, y: r.top + r.height / 2, w: r.width, h: r.height }};
                }}
            }}
            return null;
        }})()"""
    )
    assert box is not None and box["w"] > 0 and box["h"] > 0, (
        f"Could not find visible right-dock tab matching {title_candidates}"
    )
    browser.mouse_click(box["x"], box["y"])


def _verify_double_entry_and_projection(
    data_dir: Path,
    *,
    expected_fills: int,
    expected_drafts_accepted: int,
    expected_commands_accepted: int,
) -> tuple[PaperAccountView, Any]:
    """Assert authoritative ledger double-entry balance and exact view.json agreement."""
    ledger_path = data_dir / "accounts" / SUB_ACCOUNT / "ledger.sqlite"
    assert ledger_path.is_file(), f"Missing authoritative ledger: {ledger_path}"
    journal = read_journal(ledger_path)

    # 1. Double-entry accounting invariant: every entry sums to exact 0 per currency.
    assert len(journal.entries) > 0, "Expected non-empty ledger entries"
    for entry in journal.entries:
        per_ccy: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
        for posting in entry.postings:
            per_ccy[posting.currency] += Decimal(posting.amount)
        for ccy, total in per_ccy.items():
            assert total == Decimal("0"), (
                f"Unbalanced double-entry ledger at {entry.event_id} ({ccy}): {total}"
            )

    # 2. Zero duplicate fills, zero dropped drafts, zero dropped commands.
    entry_fills = [e for e in journal.events if e.kind == "ENTRY_FILLED"]
    exit_fills = [e for e in journal.events if e.kind == "EXIT_FILLED"]
    assert len(entry_fills) == 1, f"Expected exactly 1 ENTRY_FILLED, got {len(entry_fills)}"
    assert len(entry_fills) + len(exit_fills) == expected_fills, (
        f"Expected {expected_fills} total fills, got {len(entry_fills)} entry + {len(exit_fills)} exit"
    )

    draft_resolved = [
        e for e in journal.events if e.kind == "DRAFT_RESOLVED" and e.draft_receipt and e.draft_receipt.accepted
    ]
    assert len(draft_resolved) == expected_drafts_accepted, (
        f"Expected {expected_drafts_accepted} accepted DRAFT_RESOLVED, got {len(draft_resolved)}"
    )

    cmd_resolved = [e for e in journal.events if e.kind == "COMMAND_RESOLVED"]
    assert len(cmd_resolved) == expected_commands_accepted, (
        f"Expected {expected_commands_accepted} COMMAND_RESOLVED events, got {len(cmd_resolved)}"
    )
    for ev in cmd_resolved:
        parsed = json.loads(ev.reason or "{}")
        assert parsed.get("accepted") is True, f"Command not accepted in ledger: {parsed}"

    # 3. Projection view.json agrees with authoritative ledger replay.
    view_file = projection_path(data_dir, SUB_ACCOUNT)
    assert view_file.is_file(), f"Missing projection file: {view_file}"
    view = PaperAccountView.model_validate_json(view_file.read_text(encoding="utf-8"))
    assert view.sub_account_id == SUB_ACCOUNT
    assert view.ledger_seq == len(journal.events), (
        f"view.ledger_seq ({view.ledger_seq}) != journal event count ({len(journal.events)})"
    )
    assert len(view.fills) == expected_fills, f"view.fills count mismatch: {len(view.fills)}"
    assert len(view.positions) == 0, f"Expected all positions closed, got {view.positions}"
    assert len(view.rounds) == 1, f"Expected 1 round in view, got {view.rounds}"
    assert view.rounds[0].phase == "CLOSED", f"Expected round CLOSED, got {view.rounds[0]}"
    assert Decimal(view.rounds[0].quantity) == Decimal("0")

    # Verify collateral / equity balance from ledger postings matches view.json
    # (REALIZED_PNL is credited on profit / debited on loss without mutating CASH_AVAILABLE directly).
    balances: dict[tuple[str, str], Decimal] = defaultdict(lambda: Decimal("0"))
    for entry in journal.entries:
        for posting in entry.postings:
            balances[(posting.currency, posting.account)] += Decimal(posting.amount)
    ledger_collateral = collateral_balance(balances, "USDT")
    assert ledger_collateral == (
        balances[("USDT", "CASH_AVAILABLE")]
        + balances[("USDT", "MARGIN_USED")]
        + balances[("USDT", "CASH_RESERVED")]
        - balances[("USDT", "REALIZED_PNL")]
    )
    assert view.account_state is not None
    assert Decimal(view.account_state.equity) == ledger_collateral, (
        f"view equity ({view.account_state.equity}) != ledger collateral ({ledger_collateral})"
    )
    assert Decimal(view.account_state.margin_used) == Decimal("0")
    assert Decimal(view.account_state.cash_available) == (
        ledger_collateral - balances[("USDT", "MARGIN_USED")] - balances[("USDT", "CASH_RESERVED")]
    )
    return view, journal


def test_chain3_paper_trading_full_closed_loop_and_crash_recovery(tmp_path: Path) -> None:
    """Ticket 10 full closed-loop acceptance + dual-process crash recovery + 1440px dark/light UI verification."""
    assert "talaria.workbench_api" not in sys.modules, (
        "Chain 3 E2E must run without Python workbench_api"
    )

    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    log_path = tmp_path / "account.log"
    screenshot_dir = Path(
        os.environ.get("TALARIA_E2E_SCREENSHOT_DIR") or (tmp_path / "screenshots")
    )

    # 1. Prepare manual-only human strategy + paper scheme YAML (real CLI workflow).
    strategy_path = tmp_path / "manual_only.py"
    strategy_path.write_text(IDLE_STRATEGY_SOURCE, encoding="utf-8")
    reg = subprocess.run(
        [
            sys.executable,
            "-m",
            "talaria",
            "account",
            "strategy-register",
            str(strategy_path),
            "--source",
            "human",
            "--data-dir",
            str(data_dir),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(reg.stdout)["source"] == "human"

    scheme_path = tmp_path / "chain3_paper.yaml"
    scheme_path.write_text(
        f"""\
scheme: chain3-e2e
interface_version: 2
sub_accounts: [{SUB_ACCOUNT}]
slots:
  data:
    plugin: live_ws
    params:
      instrument: {INSTRUMENT}
      timeframe: 1m
      derivatives: true
      reduce_on_disconnect: true
  strategy:
    python: manual_only.py
  sizing: r_unit
  risk: seven_step
  fill: bar_sim_1m
""",
        encoding="utf-8",
    )

    # Align synthetic live replay 1m bars to current wall-clock minute.
    minute_now = (int(time.time()) // 60) * 60 * 1000
    t0 = minute_now

    # Phase 1 replay: populate tail bars around 68625 while user performs gesture -> draft -> confirm.
    replay_phase1 = tmp_path / "replay_phase1.jsonl"
    phase1_bars = [
        (400, t0 - 60_000, "68600", "68635", "68585", "68615", True, 1),
        (700, t0, "68615", "68638", "68592", "68625", False, 1),
    ]
    _write_replay_script(replay_phase1, phase1_bars)

    rust_proc: subprocess.Popen[str] | None = None
    acct_proc: subprocess.Popen[str] | None = None
    try:
        # Start ONLY compiled Rust talaria-data + Python talaria account run.
        rust_proc, conn = _start_talaria_data(data_dir, replay_phase1)
        acct_proc = _start_account_process(scheme_path, data_dir, log_path)

        # Wait until account process has connected to /ws/strategy and written initial RUNNING/ACTIVE view.json.
        view0 = _wait_for_projection(
            data_dir,
            lambda v: (
                v.process is not None
                and v.process.status == "RUNNING"
                and v.feed_status is not None
                and v.feed_status.updated_ts is not None
                and v.account_state is not None
                and v.account_state.trading_state == "ACTIVE"
            ),
            timeout=20.0,
            description="initial RUNNING/ACTIVE projection",
            proc=acct_proc,
            log_path=log_path,
        )
        assert Decimal(view0.account_state.equity) == Decimal("10000")

        with _CdpBrowser(tmp_path) as browser:
            # Open real Rust-hosted /desk SPA.
            browser.navigate(f"{conn.base_url}/desk")
            browser.wait_for_selector("[data-testid='talaria-workbench']", timeout=15.0)
            # Switch to 1×1 single-chart preset so 1m chart + right dock fill 1440x900 cleanly.
            browser.click_selector("[data-testid='preset-single']")
            browser.wait_for_selector("[data-testid='vela-host-chart-1m']", timeout=15.0)

            # Switch chart timeframe to 1m so replay bars and barriers share the same tight price band.
            browser.click_selector("[data-testid='tf-pill-chart-1m-1m']")
            browser.wait_for_condition(
                "document.querySelector('[data-testid=\"loaded-count\"]')?.textContent?.match(/[1-9]/)",
                timeout=15.0,
            )

            # Verify AccountPanel shows paper-main RUNNING online.
            _open_dock_tab(browser, ("模拟盘", "Paper"))
            browser.wait_for_selector("[data-testid='paper-account-panel']", timeout=10.0)
            browser.wait_for_condition(
                "document.querySelector('[data-testid=\"paper-process-badge\"]')?.textContent?.match(/运行中|RUNNING|在线|Online/i)",
                timeout=10.0,
            )

            # Assert zero HUMAN_DRAWING before order gesture.
            drawings_before = httpx.get(
                f"{conn.base_url}/api/drawings",
                params={"instrument_id": INSTRUMENT},
                headers=_api_headers(conn),
                timeout=5.0,
            ).json()
            assert len(drawings_before.get("items", drawings_before if isinstance(drawings_before, list) else [])) == 0

            # === STEP 1: Chart order gesture ('position') -> live Rust sizing preview ===
            browser.click_selector("[data-testid='order-tool-long-chart-1m']")
            canvas_box = browser.eval_js(
                """(() => {
                    const host = document.querySelector('[data-testid="vela-host-chart-1m"]');
                    const r = host.getBoundingClientRect();
                    return { x: r.left, y: r.top, w: r.width, h: r.height };
                })()"""
            )
            assert canvas_box and canvas_box["w"] > 200 and canvas_box["h"] > 200
            # Two clicks place a temporary LONG 'position' gesture on the chart canvas.
            anchor_x = canvas_box["x"] + canvas_box["w"] * 0.45
            anchor_y = canvas_box["y"] + canvas_box["h"] * 0.55
            browser.mouse_click(anchor_x, anchor_y)
            time.sleep(0.15)
            browser.mouse_click(anchor_x + 120, anchor_y - 70)

            browser.wait_for_selector("[data-testid='order-gesture-preview']", timeout=10.0)
            browser.wait_for_condition(
                """(() => {
                    const qty = document.querySelector('[data-testid="order-gesture-qty"]')?.textContent?.trim();
                    const rej = document.querySelector('[data-testid="order-gesture-rejected"]');
                    return (qty && qty !== '—') || Boolean(rej);
                })()""",
                timeout=10.0,
            )

            # Verify zero HUMAN_DRAWING created by the temporary order gesture!
            drawings_after_gesture = httpx.get(
                f"{conn.base_url}/api/drawings",
                params={"instrument_id": INSTRUMENT},
                headers=_api_headers(conn),
                timeout=5.0,
            ).json()
            assert len(
                drawings_after_gesture.get(
                    "items",
                    drawings_after_gesture if isinstance(drawings_after_gesture, list) else [],
                )
            ) == 0, "Temporary order gesture must NOT pollute HUMAN_DRAWING store"

            # Screenshot #1 (dark & light): Chart order gesture + Rust sizing preview + live AccountPanel.
            shot1_dark, shot1_light = browser.capture_theme_pair(
                screenshot_dir, "chain3_e2e_1_gesture_preview"
            )
            print(f"Screenshot: {shot1_dark} | Chart order gesture and sizing preview dark")
            print(f"Screenshot: {shot1_light} | Chart order gesture and sizing preview light")

            # Click "生成草案" -> opens DraftForm pre-seeded from gesture.
            browser.click_selector("[data-testid='order-gesture-create-draft']")
            browser.wait_for_selector("form input[name='entry_low']", timeout=10.0)

            # Ensure sub_account_id is set and set deterministic prices matching replay bars.
            browser.fill_input("form [name='sub_account_id']", SUB_ACCOUNT)
            browser.fill_input("form input[name='entry_low']", "68610")
            browser.fill_input("form input[name='entry_high']", "68650")
            browser.fill_input("form input[name='invalidation_stop']", "68500")
            browser.fill_input("form input[name='take_profit']", "69300")

            # Wait for live Rust sizing preview inside DraftForm to confirm valid positive quantity.
            browser.wait_for_condition(
                """(() => {
                    const qty = document.querySelector('[data-testid="paper-sizing-qty"]')?.textContent?.trim();
                    return qty && qty !== '—' && Number(qty) > 0;
                })()""",
                timeout=10.0,
            )
            preview_qty_text = browser.eval_js(
                "document.querySelector('[data-testid=\"paper-sizing-qty\"]')?.textContent?.trim()"
            )
            assert Decimal(preview_qty_text) > Decimal("0")

            # Save staged draft.
            browser.click_selector("form button[type='submit']")
            browser.wait_for_condition(
                "Boolean(document.querySelector('[data-testid^=\"draft-card-\"]'))",
                timeout=10.0,
            )
            draft_card_id = browser.eval_js(
                "document.querySelector('[data-testid^=\"draft-card-\"]')?.getAttribute('data-testid')"
            )
            draft_id = draft_card_id.removeprefix("draft-card-")

            # === STEP 2: Crash Injection #1 — kill account process around draft confirmation ===
            # Kill talaria account right BEFORE confirming draft so the confirmed draft queues in Rust,
            # then confirm in DraftPanel UI, then restart talaria account and prove zero dropped drafts.
            _stop_proc(acct_proc, force=True)
            browser.fill_input(f"[data-testid='draft-card-{draft_id}'] input[name='confirmed_by']", "trader")
            browser.click_selector(f"[data-testid='draft-card-{draft_id}'] button[type='submit']")
            browser.wait_for_condition(
                f"""(() => {{
                    const card = document.querySelector('[data-testid="draft-card-{draft_id}"]');
                    return card && /已确认|Confirmed/i.test(card.textContent || '');
                }})()""",
                timeout=10.0,
            )

            # Restart talaria-data with Phase 2 replay respecting strict 1m causality (`ts_available = ts + 60_000`).
            # Stopping and restarting talaria-data with a confirmed draft in SQLite also verifies draft queue persistence.
            _stop_proc(rust_proc, force=True)
            first_minute = _read_first_minute(data_dir)
            phase2_start_ms = int(time.time() * 1000)
            cur_min = (phase2_start_ms // 60_000) * 60_000

            phase2_bars: list[tuple[int, int, str, str, str, str, bool, int]] = [
                (300, first_minute - 60_000, "68600", "68635", "68585", "68615", True, 1),
            ]
            # 1. Confirm every minute from `first_minute` through the ready-bar minute in safe band [68590, 68640]
            #    so LiveWS becomes ready, freezes evidence cleanly, and approves the confirmed draft.
            if cur_min > first_minute:
                ready_slot = cur_min - 60_000
                fill_slot = cur_min
            else:
                ready_slot = first_minute
                fill_slot = first_minute + 60_000

            slot_ts = first_minute
            step_idx = 0
            while slot_ts <= ready_slot:
                # Fire >= 3.0s after server boot (so acct_proc #2 is subscribed) AND >= slot_ts + 60_500ms (causally closed).
                due_ms = max(3000 + step_idx * 400, (slot_ts + 60_500) - phase2_start_ms)
                phase2_bars.append((due_ms, slot_ts, "68615", "68640", "68590", "68625", True, 1))
                slot_ts += 60_000
                step_idx += 1

            ready_due_ms = max(3000 + max(0, step_idx - 1) * 400, (ready_slot + 60_500) - phase2_start_ms)
            # In-progress bar for `fill_slot` in safe band right after `ready_slot` closes so DraftEvidence covers `fill_slot`.
            phase2_bars.append((ready_due_ms + 250, fill_slot, "68620", "68640", "68595", "68625", False, 1))

            # 2. Confirm `fill_slot` and `fill_slot2 = fill_slot + 60_000` (since PLAN_SUBMITTED's `ready_at`
            #    lands ~1s inside `fill_slot`, strict point-in-time causality in bar_sim_1m requires `ts_event >= ready_at`,
            #    so `fill_slot2` is guaranteed to fill the LONG entry cleanly!).
            fill_slot2 = fill_slot + 60_000
            fill_due_ms = max(ready_due_ms + 4000, (fill_slot + 60_500) - phase2_start_ms)
            fill2_due_ms = max(fill_due_ms + 4000, (fill_slot2 + 60_500) - phase2_start_ms)
            phase2_bars.append((fill_due_ms, fill_slot, "68625", "68660", "68600", "68645", True, 2))
            phase2_bars.append((fill2_due_ms, fill_slot2, "68640", "68675", "68605", "68665", True, 1))
            phase2_bars.append((fill2_due_ms + 1500, fill_slot2, "68640", "68675", "68605", "68666", True, 2))

            # Stream fresh MARK_PRICE derivative ticks so SevenStepRisk margin valuation & unrealized PnL work live.
            phase2_marks = [
                (max(2500, ready_due_ms - 800), ready_slot + 30_000, "68625"),
                (ready_due_ms + 100, ready_slot + 59_000, "68626"),
                (fill_due_ms - 500, fill_slot + 30_000, "68645"),
                (fill_due_ms + 600, fill_slot + 59_000, "68655"),
                (fill2_due_ms - 500, fill_slot2 + 30_000, "68665"),
                (fill2_due_ms + 600, fill_slot2 + 59_000, "68675"),
            ]

            replay_phase2 = tmp_path / "replay_phase2.jsonl"
            _write_replay_script(replay_phase2, phase2_bars, marks=phase2_marks)
            rust_proc, conn = _start_talaria_data(data_dir, replay_phase2, prev_port=int(conn.base_url.rsplit(":", 1)[1]))

            # Verify confirmed draft survived talaria-data SIGKILL!
            drafts_after_crash = httpx.get(
                f"{conn.base_url}/api/paper/drafts",
                headers=_api_headers(conn),
                timeout=5.0,
            ).json()
            matching = [d for d in drafts_after_crash if d["draft_id"] == draft_id]
            assert len(matching) == 1 and matching[0]["status"] == "CONFIRMED", (
                f"Confirmed draft lost across talaria-data crash: {matching}"
            )

            # Restart talaria account: claims confirmed draft once ready_slot closes, then fills when fill_slot2 closes!
            acct_proc = _start_account_process(scheme_path, data_dir, log_path)
            fill_timeout = max(45.0, (fill2_due_ms / 1000.0) + 30.0)
            view_open = _wait_for_projection(
                data_dir,
                lambda v: len(v.positions) == 1 and Decimal(v.positions[0].quantity) > 0,
                timeout=fill_timeout,
                description="active filled position after confirmed fill_slot2 1m bar",
                proc=acct_proc,
                log_path=log_path,
            )
            active_round = view_open.positions[0]
            round_id = active_round.round_id
            initial_qty = Decimal(active_round.quantity)
            initial_stop = Decimal(active_round.stop)
            assert initial_stop == Decimal("68500")
            assert len(view_open.fills) == 1

            # Reload /desk against new talaria-data port and verify active position + chart barriers.
            browser.navigate(f"{conn.base_url}/desk")
            browser.wait_for_selector("[data-testid='vela-host-chart-1m']", timeout=15.0)
            browser.click_selector("[data-testid='preset-single']")
            browser.wait_for_selector("[data-testid='vela-host-chart-1m']", timeout=15.0)
            browser.click_selector("[data-testid='tf-pill-chart-1m-1m']")
            _open_dock_tab(browser, ("模拟盘", "Paper"))
            browser.wait_for_selector(f"[data-testid='paper-btn-close-{round_id}']", timeout=15.0)
            time.sleep(0.6)

            # Screenshot #2 (dark & light): Active position + chart EXECUTION_BARRIER + trade marker + AccountPanel.
            shot2_dark, shot2_light = browser.capture_theme_pair(
                screenshot_dir, "chain3_e2e_2_active_barriers"
            )
            print(f"Screenshot: {shot2_dark} | Active position barriers and account panel dark")
            print(f"Screenshot: {shot2_light} | Active position barriers and account panel light")

            # === STEP 3: Drag STOP barrier upward on chart canvas -> D10 confirm modal -> TIGHTEN_STOP ===
            # Scan vertically across vela-host-chart-1m's candlestick canvas from bottom to top to hit STOP (68500) first.
            host_box = browser.eval_js(
                """(() => {
                    const host = document.querySelector('[data-testid="vela-host-chart-1m"]');
                    const r = host.getBoundingClientRect();
                    return { x: r.left, y: r.top, w: r.width, h: r.height };
                })()"""
            )
            probe_x = host_box["x"] + host_box["w"] * 0.45
            stop_y: float | None = None
            # Main candlestick pane occupies roughly the top 62% of the chart host (RSI/ATR below).
            scan_bottom = int(host_box["h"] * 0.62)
            for offset_y in range(scan_bottom, 25, -4):
                cy = host_box["y"] + offset_y
                browser.mouse_move(probe_x, cy)
                cursor = browser.eval_js(
                    f"document.elementFromPoint({probe_x}, {cy})?.style?.cursor || ''"
                )
                if cursor == "ns-resize":
                    stop_y = cy
                    break
            assert stop_y is not None, (
                "Could not hit draggable STOP barrier line (`cursor: ns-resize`) on chart canvas"
            )

            # Drag STOP line upward (smaller Y = higher price = TIGHTEN_STOP for LONG).
            browser.mouse_drag(probe_x, stop_y, probe_x, stop_y - 30, steps=10)
            browser.wait_for_selector("[data-testid='barrier-drag-confirm-dialog']", timeout=10.0)
            browser.wait_for_selector("[data-testid='barrier-drag-direct-badge']", timeout=5.0)
            browser.fill_input("[data-testid='barrier-drag-reason-input']", "e2e tighten stop drag")
            browser.click_selector("[data-testid='barrier-drag-submit']")

            view_tightened = _wait_for_projection(
                data_dir,
                lambda v: (
                    len(v.positions) == 1
                    and Decimal(v.positions[0].stop) > initial_stop
                ),
                timeout=15.0,
                description="tightened stop reflected in view.json",
                proc=acct_proc,
                log_path=log_path,
            )
            tightened_stop = Decimal(view_tightened.positions[0].stop)
            assert tightened_stop > initial_stop

            # === STEP 4: Crash Injection #2 — SIGKILL talaria account while holding open position ===
            _stop_proc(acct_proc, force=True)
            # Wait for heartbeat timeout (>5s) and confirm Rust API marks process OFFLINE.
            offline_deadline = time.monotonic() + 12.0
            saw_offline = False
            while time.monotonic() < offline_deadline:
                accts_resp = httpx.get(
                    f"{conn.base_url}/api/paper/accounts",
                    headers=_api_headers(conn),
                    timeout=3.0,
                ).json()
                assert SUB_ACCOUNT in accts_resp, f"Expected {SUB_ACCOUNT} in /api/paper/accounts: {accts_resp}"
                acct_view = httpx.get(
                    f"{conn.base_url}/api/paper/accounts/{SUB_ACCOUNT}",
                    headers=_api_headers(conn),
                    timeout=3.0,
                ).json()
                if (acct_view.get("process") or {}).get("status") == "OFFLINE":
                    saw_offline = True
                    break
                time.sleep(0.4)
            assert saw_offline, "Expected talaria-data to report process.status == OFFLINE after SIGKILL"

            # Restart talaria account -> verify recovery to RUNNING with intact position & stop.
            acct_proc = _start_account_process(scheme_path, data_dir, log_path)
            view_recovered = _wait_for_projection(
                data_dir,
                lambda v: (
                    v.process is not None
                    and v.process.status == "RUNNING"
                    and len(v.positions) == 1
                    and Decimal(v.positions[0].stop) == tightened_stop
                ),
                timeout=15.0,
                description="post-crash RUNNING recovery with intact tightened stop",
                proc=acct_proc,
                log_path=log_path,
            )
            assert Decimal(view_recovered.positions[0].quantity) == initial_qty
            assert len(view_recovered.fills) == 1, "Crash recovery must NOT duplicate ENTRY fill"

            # Wait for AccountPanel to reflect RUNNING again so Reduce 50% button is enabled.
            browser.wait_for_condition(
                f"""(() => {{
                    const btn = document.querySelector('[data-testid="paper-btn-reduce-{round_id}"]');
                    return Boolean(btn) && !btn.disabled;
                }})()""",
                timeout=10.0,
            )

            # === STEP 5: Crash Injection #3 — submit REDUCE_RATIO (50%), SIGKILL & restart talaria-data ===
            # Open the confirm dialog while acct_proc is live and layout is stable (opening modal sends no request).
            browser.click_selector(f"[data-testid='paper-btn-reduce-{round_id}']")
            browser.wait_for_selector("[data-testid='paper-confirm-dialog']", timeout=10.0)
            # Ensure heartbeat_ts is fresh (<2s old) before SIGKILL so clicking Confirm lands within the 5s online window,
            # leaving REDUCE_RATIO queued (claimed=False, receipt=None) in Rust SQLite across talaria-data SIGKILL.
            _wait_for_projection(
                data_dir,
                lambda v: (
                    v.process is not None
                    and v.process.status == "RUNNING"
                    and (int(time.time() * 1000) - v.process.heartbeat_ts) < 2_000
                ),
                timeout=10.0,
                description="fresh heartbeat before Crash Injection #3",
                proc=acct_proc,
                log_path=log_path,
            )
            _stop_proc(acct_proc, force=True)
            browser.click_selector("[data-testid='paper-confirm-submit']")
            browser.wait_for_condition(
                "!document.querySelector('[data-testid=\"paper-confirm-dialog\"]')",
                timeout=10.0,
            )

            # Verify command is persisted in Rust SQLite before killing talaria-data.
            cmds_before = httpx.get(
                f"{conn.base_url}/api/paper/commands",
                params={"sub_account_id": SUB_ACCOUNT},
                headers=_api_headers(conn),
                timeout=5.0,
            ).json()
            reduce_cmds = [c for c in cmds_before if c["command"]["kind"] == "REDUCE_RATIO"]
            assert len(reduce_cmds) == 1 and not reduce_cmds[0]["claimed"] and reduce_cmds[0]["receipt"] is None
            reduce_cmd_id = reduce_cmds[0]["command"]["command_id"]

            # SIGKILL talaria-data and restart on a new port with Phase 3 replay.
            _stop_proc(rust_proc, force=True)
            replay_phase3 = tmp_path / "replay_phase3.jsonl"
            phase3_bars = [
                (400, fill_slot2, "68640", "68675", "68605", "68665", True, 1),
                (700, fill_slot2 + 60_000, "68665", "68695", "68635", "68680", False, 1),
            ]
            phase3_marks = [
                (2500, fill_slot2 + 65_000, "68680"),
                (4500, fill_slot2 + 70_000, "68685"),
                (7000, fill_slot2 + 75_000, "68686"),
                (10000, fill_slot2 + 80_000, "68688"),
            ]
            _write_replay_script(replay_phase3, phase3_bars, marks=phase3_marks)
            rust_proc, conn = _start_talaria_data(data_dir, replay_phase3, prev_port=int(conn.base_url.rsplit(":", 1)[1]))

            # Verify queued REDUCE_RATIO command survived talaria-data SIGKILL!
            cmds_after = httpx.get(
                f"{conn.base_url}/api/paper/commands",
                params={"sub_account_id": SUB_ACCOUNT},
                headers=_api_headers(conn),
                timeout=5.0,
            ).json()
            surviving = [c for c in cmds_after if c["command"]["command_id"] == reduce_cmd_id]
            assert len(surviving) == 1 and not surviving[0]["claimed"] and surviving[0]["receipt"] is None, (
                f"Queued REDUCE_RATIO lost across talaria-data crash: {surviving}"
            )

            # Restart talaria account -> reconnects to new Rust port, executes REDUCE_RATIO (50%)!
            acct_proc = _start_account_process(scheme_path, data_dir, log_path)
            view_reduced = _wait_for_projection(
                data_dir,
                lambda v: (
                    len(v.positions) == 1
                    and Decimal(v.positions[0].quantity) < initial_qty
                    and len(v.fills) == 2
                ),
                timeout=20.0,
                description="50% reduced position after talaria-data crash recovery",
                proc=acct_proc,
                log_path=log_path,
            )
            reduced_qty = Decimal(view_reduced.positions[0].quantity)
            assert Decimal("0") < reduced_qty < initial_qty
            # Check ~50% reduction (allowing instrument lot step rounding).
            assert abs(reduced_qty - (initial_qty / Decimal("2"))) <= Decimal("0.002")

            # === STEP 6: Close All via AccountPanel UI ===
            browser.navigate(f"{conn.base_url}/desk")
            browser.wait_for_selector("[data-testid='talaria-workbench']", timeout=15.0)
            browser.click_selector("[data-testid='preset-single']")
            _open_dock_tab(browser, ("模拟盘", "Paper"))
            browser.wait_for_condition(
                f"""(() => {{
                    const btn = document.querySelector('[data-testid="paper-btn-close-{round_id}"]');
                    return Boolean(btn) && !btn.disabled;
                }})()""",
                timeout=15.0,
            )

            close_start = time.monotonic()
            browser.click_selector(f"[data-testid='paper-btn-close-{round_id}']")
            browser.wait_for_selector("[data-testid='paper-confirm-dialog']", timeout=10.0)
            browser.click_selector("[data-testid='paper-confirm-submit']")

            view_closed = _wait_for_projection(
                data_dir,
                lambda v: len(v.positions) == 0 and len(v.rounds) == 1 and v.rounds[0].phase == "CLOSED",
                timeout=15.0,
                description="closed round after CLOSE_ALL",
                proc=acct_proc,
                log_path=log_path,
            )
            close_elapsed = time.monotonic() - close_start
            assert len(view_closed.fills) == 3

            # Wait for AccountPanel UI to reflect empty positions + closed round + 3 fills.
            browser.wait_for_selector("[data-testid='paper-positions-empty']", timeout=10.0)
            browser.wait_for_selector("[data-testid='paper-rounds-list']", timeout=10.0)
            browser.wait_for_selector("[data-testid='paper-fills-list']", timeout=10.0)
            browser.eval_js(
                "document.querySelector('[data-testid=\"paper-rounds-section\"]')?.scrollIntoView({ block: 'start' })"
            )
            time.sleep(0.4)

            # Screenshot #3 (dark & light): Closed round + 3 fills + updated equity in AccountPanel.
            shot3_dark, shot3_light = browser.capture_theme_pair(
                screenshot_dir, "chain3_e2e_3_closed_round"
            )
            print(f"Screenshot: {shot3_dark} | Closed round and fills in account panel dark")
            print(f"Screenshot: {shot3_light} | Closed round and fills in account panel light")
            print(f"CLOSE_ALL UI->projection round-trip: {close_elapsed:.2f}s")
            assert browser.page_errors == [], f"Uncaught browser JS errors: {browser.page_errors}"

        # Cleanly stop account process and run full ledger/projection double-entry audit.
        _stop_proc(acct_proc, force=False)
        acct_proc = None
        _verify_double_entry_and_projection(
            data_dir,
            expected_fills=3,
            expected_drafts_accepted=1,
            expected_commands_accepted=3,
        )
        assert "talaria.workbench_api" not in sys.modules, (
            "Python workbench_api must never be imported during Chain 3 E2E"
        )
    finally:
        _stop_proc(acct_proc, force=True)
        _stop_proc(rust_proc, force=True)
