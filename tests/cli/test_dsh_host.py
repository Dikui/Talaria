"""DSH 宿主子进程：缺失、健康探针、日志脱敏、优雅退出与桥接凭据。"""

from __future__ import annotations

import fcntl
import json
import os
import signal
import stat
import sys
import threading
import time
import urllib.request
from contextlib import nullcontext
from http.client import BadStatusLine, InvalidURL
from pathlib import Path
from types import SimpleNamespace

import pytest

from talaria.cli.dsh_host import (
    DshHost,
    DshHostError,
    bridge_descriptor_path,
    resolve_dsh_bin,
    write_bridge_descriptor,
)

pytestmark = pytest.mark.medium


def _fake_host_script(tmp_path: Path, body: str) -> Path:
    script = tmp_path / "fake_dsh.py"
    script.write_text(body, encoding="utf-8")
    wrapper = tmp_path / "fake-dsh"
    wrapper.write_text(
        f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding="utf-8"
    )
    wrapper.chmod(0o755)
    return wrapper


def _host(tmp_path: Path, script: Path, *, timeout: float = 15.0, on_offline=None) -> DshHost:
    return DshHost(
        dsh_bin=str(script),
        profile="talaria-desk",
        host="127.0.0.1",
        port=1,
        bridge_path=bridge_descriptor_path(tmp_path / "data"),
        log_dir=tmp_path / "logs",
        startup_timeout=timeout,
        on_offline=on_offline,
    )


def test_missing_dsh_binary_reports_install_hint(tmp_path: Path) -> None:
    with pytest.raises(DshHostError) as caught:
        resolve_dsh_bin(str(tmp_path / "missing-dsh"))
    message = str(caught.value)
    assert "DSH 可执行文件不存在" in message
    assert "--solo" in message


def test_host_starts_probes_and_stops_with_redacted_log(tmp_path: Path) -> None:
    script = _fake_host_script(
        tmp_path,
        """
import http.server
import sys

class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")
    def log_message(self, *args):
        pass

server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
print(f"dsh web: http://127.0.0.1:{server.server_port}/?token=super-secret", flush=True)
server.serve_forever()
""",
    )
    host = _host(tmp_path, script)
    info = host.start()
    try:
        assert info.state == "online"
        assert info.url is not None and "token=super-secret" in info.url
        assert host.probe() is True
        assert host.status_payload()["host"] == "online"
        api_url = host.status_payload()["url"]
        assert api_url.startswith("http://127.0.0.1:")
        assert "token=" not in api_url
        assert host.info().base_url == api_url
        assert host.info().base_url == info.url.split("?", 1)[0]
        log = Path(info.log_path).read_text(encoding="utf-8")
        assert "dsh web:" in log
        assert "super-secret" not in log
        assert "token=***" in log
    finally:
        host.stop()
    assert host.info().state == "stopped"
    assert host.probe() is False


def test_start_failure_includes_recent_output(tmp_path: Path) -> None:
    script = _fake_host_script(
        tmp_path,
        """
import sys
print("startup exploded", file=sys.stderr, flush=True)
sys.exit(3)
""",
    )
    host = _host(tmp_path, script, timeout=5.0)
    with pytest.raises(DshHostError) as caught:
        host.start()
    message = str(caught.value)
    assert "DSH 协同宿主启动失败" in message
    assert "startup exploded" in message
    assert "dsh-install" in message
    assert "--patch" not in host.command()


def test_bridge_descriptor_is_private_and_round_trips(tmp_path: Path) -> None:
    path = bridge_descriptor_path(tmp_path / "data")
    write_bridge_descriptor(
        path, base_url="http://127.0.0.1:8765", token="t0ken", daemon_pid=42
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["base_url"] == "http://127.0.0.1:8765"
    assert payload["token"] == "t0ken"
    assert payload["daemon_pid"] == 42
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_mark_unavailable_reports_offline_without_process(tmp_path: Path) -> None:
    host = _host(tmp_path, tmp_path / "unused-dsh")
    host.mark_unavailable("未找到 DSH 可执行文件")
    payload = host.status_payload()
    assert payload["host"] == "offline"
    assert "未找到 DSH" in str(payload["detail"])
    assert payload["pid"] is None


def _wait_for_state(host: DshHost, state: str, *, detail: str = ""):
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        info = host.info()
        if info.state == state and detail in info.detail:
            return info
        time.sleep(0.02)
    pytest.fail(f"期望 DSH 状态为 {state}，实际为 {host.info().state}")


@pytest.mark.regression
@pytest.mark.parametrize("error", [BadStatusLine, InvalidURL])
def test_host_retries_after_health_probe_exception(tmp_path, monkeypatch, error):
    # platform/03: replace the HTTP connection with a malformed first response.
    url = "http://127.0.0.1:1/?token=probe-secret"
    script = _fake_host_script(tmp_path, f"import time\nprint({url!r}, flush=True)\ntime.sleep(60)\n")
    failed = threading.Event()
    probe_times = []

    def open_probe(self, request, *, timeout):
        probe_times.append(time.monotonic())
        if len(probe_times) == 1:
            raise error(request.full_url)
        return nullcontext(SimpleNamespace(status=200))

    monkeypatch.setattr(urllib.request.OpenerDirector, "open", open_probe)
    host = _host(tmp_path, script, on_offline=lambda info: failed.set())
    host.supervise()
    try:
        assert failed.wait(3), "健康探针异常后未报告离线"
        info = host.info()
        assert info.state == "offline"
        assert "健康探针" in info.detail
        assert "token=***" in info.detail
        assert "probe-secret" not in info.detail
        _wait_for_state(host, "online")
        assert probe_times[1] - probe_times[0] >= 1.0
        assert "probe-secret" not in host.log_path.read_text()
    finally:
        host.stop()


@pytest.mark.regression
def test_host_stop_kills_remaining_group_after_leader_exits(tmp_path, monkeypatch):
    # platform/03: the real descendant holds a lock until it receives SIGKILL.
    held_by_child = tmp_path / "held-by-dsh"
    script = _fake_host_script(
        tmp_path,
        f"""
import fcntl
import os
import signal
import time

if os.fork() == 0:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    with open({str(held_by_child)!r}, "w") as locked:
        fcntl.flock(locked, fcntl.LOCK_EX)
        print("http://127.0.0.1:1/?token=example", flush=True)
        time.sleep(60)
else:
    time.sleep(60)
""",
    )
    monkeypatch.setattr(DshHost, "probe", lambda self: True)
    host = _host(tmp_path, script)
    info = host.start()
    try:
        host.stop(timeout=0.2)
        with held_by_child.open() as locked:
            fcntl.flock(locked, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert host.info().state == "stopped"
    finally:
        try:
            os.killpg(info.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


@pytest.mark.spec
def test_host_restarts_with_backoff_and_republishes_private_entry(tmp_path, monkeypatch):
    # platform/03: use the M2 fake DSH with a scripted server and injected HTTP probe.
    fixture = Path(__file__).resolve().parents[1] / "fixtures/m2/fake_dsh_host.py"
    attempts = tmp_path / "attempts"
    recording = tmp_path / "recording.jsonl"
    recording.touch()
    monkeypatch.setenv("TALARIA_M2_RECORDING", str(recording))
    monkeypatch.setenv("TALARIA_M2_LOG", str(tmp_path / "m2.log"))
    script = _fake_host_script(
        tmp_path,
        f"""
import runpy
import sys
import time
from pathlib import Path

class ScriptedServer:
    def __init__(self, address, handler):
        path = Path({str(attempts)!r})
        attempt = len(path.read_text().splitlines()) if path.exists() else 0
        with path.open("a") as log:
            print(time.monotonic(), file=log)
        if attempt == 1:
            print("failed http://127.0.0.1:1/?token=example", flush=True)
            sys.exit(7)

    def serve_forever(self, poll_interval):
        time.sleep(60)

    def server_close(self):
        pass

sys.exit(runpy.run_path({str(fixture)!r})["main"](sys.argv[1:], server_factory=ScriptedServer))
""",
    )
    monkeypatch.setattr(DshHost, "probe", lambda self: True)
    host = _host(tmp_path, script)
    runtime = tmp_path / "data/runtime"
    bridge = write_bridge_descriptor(
        runtime / "dsh-bridge.json", base_url="http://127.0.0.1:8765",
        token="bridge", daemon_pid=os.getpid(),
    )
    entry = runtime / "dsh-entry.json"
    host.supervise()
    try:
        first = _wait_for_state(host, "online")
        assert json.loads(entry.read_text())["url"] == "http://127.0.0.1:1/?token=recorded"
        assert json.loads(entry.read_text())["pgid"] == os.getpgid(first.pid)
        assert json.loads(bridge.read_text())["pgid"] == os.getpgid(first.pid)
        assert stat.S_IMODE(entry.stat().st_mode) == 0o600
        assert stat.S_IMODE(runtime.stat().st_mode) == 0o700
        crashed_at = time.monotonic()
        os.kill(first.pid, signal.SIGKILL)
        _wait_for_state(host, "offline", detail="意外退出")
        assert not entry.exists()
        last_failure = _wait_for_state(host, "offline", detail="退出码 7")
        assert last_failure.pid is None
        assert not entry.exists()
        recovered = _wait_for_state(host, "online")
        assert recovered.pid != first.pid
        assert json.loads(entry.read_text())["pid"] == recovered.pid
        _, second_start, third_start = map(float, attempts.read_text().splitlines())
        assert second_start - crashed_at >= 1.0
        assert third_start - second_start >= 2.0
        assert "token=recorded" not in Path(recovered.log_path).read_text()
        assert "token=example" not in last_failure.detail
        assert bridge.exists()
    finally:
        host.stop()
    assert not entry.exists()
