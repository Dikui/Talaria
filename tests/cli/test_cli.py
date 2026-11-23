"""CLI 参数边界：单机/双进程、回环地址、端口与退出码。"""

from __future__ import annotations

import json
import os
import plistlib
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from io import StringIO
from pathlib import Path

import pytest
import uvicorn
from fastapi.testclient import TestClient

from talaria.cli import dsh_host
from talaria.cli import service as service_cli
from talaria.cli.dsh_host import DshHost, bridge_descriptor_path, dsh_entry_path
from talaria.cli.main import main, validate_loopback_host
from talaria.workbench_api.service_config import FeatureToggles
from talaria.workbench_api.service_descriptor import (
    read_service_descriptor,
    service_descriptor_path,
    write_service_descriptor,
)

pytestmark = pytest.mark.small


@pytest.fixture(autouse=True)
def isolate_service_profiles(tmp_path, monkeypatch):
    # 回放测试检查旧作业和桌面 profile，替代真实 launchctl 与用户配置目录。
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("DSH_HOME", str(tmp_path / "dsh-home"))
    monkeypatch.setattr(
        service_cli, "_run_launchctl", lambda args: subprocess.CompletedProcess(args, 113)
    )


def test_up_dual_rejects_same_port(capsys) -> None:
    assert main(["up", "--port", "8765", "--dsh-port", "8765"]) == 2
    assert "不能与 Talaria 守护进程端口相同" in capsys.readouterr().err


def test_up_rejects_non_loopback_host(capsys) -> None:
    assert main(["up", "--solo", "--host", "0.0.0.0"]) == 2
    assert "只允许绑定回环地址" in capsys.readouterr().err


def test_up_rejects_invalid_port(capsys) -> None:
    assert main(["up", "--solo", "--port", "0"]) == 2
    assert "端口必须在 1..65535" in capsys.readouterr().err


# 任务单 04：两个命令都列出已注册的市场包。
@pytest.mark.spec
@pytest.mark.parametrize(
    "arguments",
    [
        ["up"],
        [
            "evaluate-levels", "--instrument", "TEST", "--timeframe", "1m",
            "--from", "0", "--to", "60000",
        ],
    ],
)
def test_unknown_market_pack_lists_choices(arguments, capsys) -> None:
    with pytest.raises(SystemExit) as error:
        main([*arguments, "--pack", "unknown-market"])
    assert error.value.code == 2
    stderr = capsys.readouterr().err
    assert "unknown-market" in stderr
    assert "crypto_perp" in stderr


@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.2", "::1"])
def test_loopback_hosts_are_accepted(host: str) -> None:
    assert validate_loopback_host(host) == host


@pytest.mark.regression
@pytest.mark.parametrize(
    ("host", "url_host", "canonical_host", "canonical_url_host"),
    [
        ("127.0.0.2", "127.0.0.2", "127.0.0.2", "127.0.0.2"),
        ("0:0:0:0:0:0:0:1", "[0:0:0:0:0:0:0:1]", "::1", "[::1]"),
    ],
)
def test_up_loopback_host_can_be_published_in_service_descriptor(
    tmp_path, host, url_host, canonical_host, canonical_url_host
):
    # platform/01: CLI 绑定地址和磁盘接口描述采用相同的回环范围与规范化。
    assert validate_loopback_host(host) == canonical_host
    path = service_descriptor_path(tmp_path)
    write_service_descriptor(
        path,
        base_url=f"http://{url_host}:8765/",
        token="test-token",
        pid=os.getpid(),
        features=FeatureToggles(),
    )
    descriptor = read_service_descriptor(tmp_path)
    assert descriptor is not None
    assert descriptor.base_url == f"http://{canonical_url_host}:8765"
    assert json.loads(path.read_text())["base_url"] == descriptor.base_url


def test_contract_check_has_zero_drift(capsys) -> None:
    assert main(["contract-check"]) == 0
    assert "零漂移" in capsys.readouterr().out


def test_up_rejects_missing_replay_file(capsys, tmp_path) -> None:
    missing = tmp_path / "nope.jsonl"
    assert main(["up", "--solo", "--replay-events", str(missing)]) == 2
    assert "回放文件不存在" in capsys.readouterr().err


def test_up_rejects_replay_directory(capsys, tmp_path) -> None:
    assert main(["up", "--solo", "--replay-events", str(tmp_path)]) == 2
    assert "回放文件不存在" in capsys.readouterr().err


@pytest.mark.parametrize("speed", ["0", "-1", "-2.5"])
def test_up_rejects_non_positive_replay_speed(capsys, tmp_path, speed: str) -> None:
    replay = tmp_path / "demo.jsonl"
    replay.write_text('{"offset_ms": 0, "type": "heartbeat"}\n', encoding="utf-8")
    assert main(["up", "--solo", "--replay-events", str(replay), "--replay-speed", speed]) == 2
    assert "回放速度必须为正数" in capsys.readouterr().err

# data-access/02: protected directories cannot be modified by replay.
@pytest.mark.spec
def test_replay_requires_explicit_isolated_data_dir(tmp_path, monkeypatch, capsys) -> None:
    import importlib

    cli = importlib.import_module("talaria.cli.main")
    daily = tmp_path / "daily"
    daily.mkdir()
    sentinel = daily / "keep"
    sentinel.write_bytes(b"daily data")
    alias = tmp_path / "alias"
    alias.symlink_to(daily, target_is_directory=True)
    monkeypatch.setenv("TALARIA_DATA_DIR", str(daily))
    replay = tmp_path / "replay.jsonl"
    replay.write_text('{"offset_ms": 0, "type": "heartbeat"}\n', encoding="utf-8")
    args = ["up", "--solo", "--replay-events", str(replay)]

    def fake_run_up(args, *, settings, **kwargs):
        if settings.data_dir != tmp_path / "isolated":
            return 99
        (settings.data_dir / "recorded").write_bytes(b"replay output")
        return 0

    monkeypatch.setattr(cli, "_run_up", fake_run_up)
    assert main(args) == 2
    assert "显式指定 --data-dir" in capsys.readouterr().err
    for protected in (daily, alias, daily / "nested"):
        assert main([*args, "--data-dir", str(protected)]) == 2
        assert "日常数据目录" in capsys.readouterr().err
    assert main([*args, f"--data-dir={tmp_path / 'isolated'}"]) == 0
    assert (tmp_path / "isolated" / "recorded").read_bytes() == b"replay output"
    assert list(daily.iterdir()) == [sentinel]
    assert sentinel.read_bytes() == b"daily data"


# data-access/02: an installed launchd directory is protected even if the job is stopped.
@pytest.mark.spec
def test_replay_rejects_default_and_launchd_data_dir(tmp_path, monkeypatch, capsys) -> None:
    from talaria.cli.service import DEFAULT_SERVICE_LABEL

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("TALARIA_DATA_DIR", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    agents = tmp_path / "Library" / "LaunchAgents"
    agents.mkdir(parents=True)
    (agents / f"{DEFAULT_SERVICE_LABEL}.plist").write_bytes(plistlib.dumps({
        "ProgramArguments": ["/bin/talaria", "up", "--data-dir=resident"],
        "WorkingDirectory": str(tmp_path),
    }))
    replay = tmp_path / "replay.jsonl"
    replay.write_text('{"offset_ms": 0, "type": "heartbeat"}\n', encoding="utf-8")
    monkeypatch.setattr("talaria.cli.main._run_up", lambda *args, **kwargs: 99)
    for protected in (".talaria", "resident"):
        assert main(["up", "--solo", "--replay-events", str(replay), "--data-dir", protected]) == 2
        assert "日常数据目录" in capsys.readouterr().err
        assert not (tmp_path / protected).exists()


# data-access/02: the loaded job may still point to a previous service directory.
@pytest.mark.spec
def test_replay_protects_data_dir_of_loaded_launchd_job(tmp_path, monkeypatch, capsys) -> None:
    daily = tmp_path / "service-root"
    replay = tmp_path / "replay.jsonl"
    replay.write_text('{"offset_ms": 0, "type": "heartbeat"}\n', encoding="utf-8")
    launchctl = (
        f"working directory = {tmp_path}\n"
        "arguments = {\n\t/bin/talaria\n\tup\n\t--data-dir\n\tservice-root\n}\n"
    )
    monkeypatch.setattr(
        "talaria.cli.service._run_launchctl",
        lambda args: subprocess.CompletedProcess(args, 0, launchctl, ""),
    )
    monkeypatch.setattr("talaria.cli.main._run_up", lambda *args, **kwargs: 99)

    assert main([
        "up", "--solo", "--replay-events", str(replay), "--data-dir", str(daily),
    ]) == 2
    assert "日常数据目录" in capsys.readouterr().err
    assert not daily.exists()


# platform/05: 即使桌面应用未启动，回放也不得使用桌面插件的日常数据目录。
@pytest.mark.spec
def test_replay_protects_data_dir_configured_in_desktop_plugin(
    tmp_path, repo_root, monkeypatch, capsys
):
    daily = tmp_path / "desktop-data"
    profile = dsh_host.dsh_profile_dir(dsh_host.DESKTOP_PROFILE)
    profile.mkdir(parents=True)
    dsh_host.install_talaria_patch(
        profile, repo_root / "dsh/packages/talaria-dsh-plugin", daily,
        service={"cwd": str(daily)},
    )
    replay = tmp_path / "replay.jsonl"
    replay.write_text('{"offset_ms": 0, "type": "heartbeat"}\n', encoding="utf-8")
    monkeypatch.setattr("talaria.cli.main._run_up", lambda *args, **kwargs: 99)

    assert main([
        "up", "--solo", "--replay-events", str(replay), "--data-dir", str(daily / "nested"),
    ]) == 2
    assert "日常数据目录" in capsys.readouterr().err
    assert not daily.exists()


@pytest.mark.regression
@pytest.mark.parametrize("daily,replay_dir", [
    ("Talaria/Data", "talaria/data"),
    ("Talaria/Data", "talaria/data/nested"),
    ("Talaria/Data/nested", "talaria/data"),
])
def test_replay_rejects_casefolded_directory_overlap(tmp_path, monkeypatch, capsys, daily, replay_dir):
    # Ticket 02: paths can overlap on a case-insensitive volume before directories exist.
    monkeypatch.setenv("TALARIA_DATA_DIR", str(tmp_path / daily))
    monkeypatch.setattr("talaria.cli.main._run_up", lambda *args, **kwargs: 99)
    replay = tmp_path / "events.jsonl"
    replay.write_text('{"offset_ms": 0, "type": "heartbeat"}\n', encoding="utf-8")

    assert main([
        "up", "--solo", "--replay-events", str(replay), "--data-dir", str(tmp_path / replay_dir),
    ]) == 2
    assert "日常数据目录" in capsys.readouterr().err


@pytest.mark.regression
def test_replay_rejects_existing_paths_with_same_directory_identity(tmp_path, monkeypatch, capsys):
    # Ticket 02: simulate different normalized names referring to one filesystem directory.
    daily, alias = tmp_path / "daily", tmp_path / "alias"
    daily.mkdir()
    alias.mkdir()
    monkeypatch.setenv("TALARIA_DATA_DIR", str(daily))
    monkeypatch.setattr("talaria.cli.main._run_up", lambda *args, **kwargs: 99)
    samefile = os.path.samefile

    def same_directory(left, right):
        return {Path(left), Path(right)} == {daily, alias} or samefile(left, right)

    monkeypatch.setattr(os.path, "samefile", same_directory)
    replay = tmp_path / "events.jsonl"
    replay.write_text('{"offset_ms": 0, "type": "heartbeat"}\n', encoding="utf-8")

    assert main([
        "up", "--solo", "--replay-events", str(replay), "--data-dir", str(alias),
    ]) == 2
    assert "日常数据目录" in capsys.readouterr().err



def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _get(url: str, *, token: str | None = None) -> tuple[int, bytes]:
    request = urllib.request.Request(url)
    if token is not None:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=2.0) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except (urllib.error.URLError, OSError):
        return 0, b""


def _wait_for(predicate, timeout: float = 15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.1)
    raise AssertionError("等待超时")


def _install_test_dsh(executable: Path) -> None:
    executable.write_text(
        '#!/bin/sh\ntest -s "$TALARIA_DSH_BRIDGE" || exit 5\n'
        "echo 'http://127.0.0.1:8766/?token=example'\nexec /bin/sleep 60\n"
    )
    executable.chmod(0o755)


@pytest.mark.regression
@pytest.mark.parametrize("descriptor_name", ["dsh-entry.json", "dsh-bridge.json"])
def test_up_cleans_orphaned_dsh_group_before_serving(tmp_path, monkeypatch, descriptor_name):
    # platform/03: use real child processes and replace only the Uvicorn listener.
    with subprocess.Popen([sys.executable, "-c", "pass"]) as daemon:
        daemon.wait(timeout=3)
    term_notice = tmp_path / "term"
    group = subprocess.Popen(
        [
            sys.executable, "-c",
            "import signal, sys, time\n"
            "from pathlib import Path\n"
            "signal.signal(signal.SIGTERM, lambda *_: Path(sys.argv[1]).touch())\n"
            "print('ready', flush=True)\n"
            "time.sleep(60)\n",
            str(term_notice),
        ],
        stdout=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        assert group.stdout.readline().strip() == "ready"
        runtime = tmp_path / "runtime"
        runtime.mkdir()
        (runtime / descriptor_name).write_text(json.dumps({
            "daemon_pid": daemon.pid,
            "pgid": group.pid,
        }))

        def run_server(server):
            assert term_notice.exists()
            assert group.wait(timeout=3) == -signal.SIGKILL
            server.started = True

        monkeypatch.setattr(uvicorn.Server, "run", run_server)
        assert main(["up", "--solo", "--data-dir", str(tmp_path)]) == 0
    finally:
        if group.poll() is None:
            group.kill()
        group.communicate(timeout=3)


@pytest.mark.regression
@pytest.mark.parametrize(
    ("obstacle", "detail"),
    [("binary", "DSH 可执行文件不存在"), ("bridge", "无法写入 DSH 桥接凭据")],
    ids=["missing-binary", "unwritable-bridge"],
)
def test_up_retries_dsh_when_first_attempt_cannot_prepare(
    tmp_path, monkeypatch, obstacle, detail
):
    # platform/03: exercise the real filesystem and CLI with an in-process API.
    bridge = bridge_descriptor_path(tmp_path)
    executable = tmp_path / "dsh"

    if obstacle == "bridge":
        _install_test_dsh(executable)
        bridge.mkdir(parents=True)

    def run_server(server):
        server.started = True
        client = TestClient(server.config.app)

        def status(state):
            payload = client.get("/api/collaboration").json()
            return payload if payload["host"] == state else None

        assert detail in _wait_for(lambda: status("offline"), timeout=3)["detail"]
        if obstacle == "binary":
            _install_test_dsh(executable)
        else:
            bridge.rmdir()
        assert _wait_for(lambda: status("online"), timeout=3)["pid"] > 0
        descriptor = read_service_descriptor(tmp_path)
        assert json.loads(bridge.read_text())["token"] == descriptor.token

    monkeypatch.setattr(DshHost, "probe", lambda self: True)
    monkeypatch.setattr(uvicorn.Server, "run", run_server)
    assert main(["up", "--data-dir", str(tmp_path), "--dsh-bin", str(executable)]) == 0


@pytest.mark.regression
@pytest.mark.parametrize(
    "message", ["Talaria 看盘", "DSH 协同宿主：离线"], ids=["ready", "offline"]
)
def test_up_retries_and_stops_when_stderr_print_fails(tmp_path, monkeypatch, message):
    # platform/03: replace the stderr stream and serve the API without TCP.
    failed_write = threading.Event()

    class BrokenStderr(StringIO):
        def write(self, text):
            if text.startswith(message):
                failed_write.set()
                raise BrokenPipeError("stderr closed")
            return super().write(text)

    stderr = BrokenStderr()
    monkeypatch.setattr(sys, "stderr", stderr)
    executable = tmp_path / "dsh"
    entry = dsh_entry_path(tmp_path)

    def run_server(server):
        server.started = True
        client = TestClient(server.config.app)
        _wait_for(
            lambda: client.get("/api/collaboration").json()["host"] == "offline",
            timeout=3,
        )
        assert failed_write.wait(3)
        _install_test_dsh(executable)
        _wait_for(entry.is_file, timeout=3)

    monkeypatch.setattr(DshHost, "probe", lambda self: True)
    monkeypatch.setattr(uvicorn.Server, "run", run_server)
    result = main(["up", "--data-dir", str(tmp_path), "--dsh-bin", str(executable)])
    assert result == 0, stderr.getvalue()
    assert not entry.exists()
    assert not bridge_descriptor_path(tmp_path).exists()


def test_up_dual_keeps_fast_path_when_dsh_fails(tmp_path: Path) -> None:
    """DSH 起不来时，Python 守护进程、健康探针、快照与 /desk 仍可用。"""
    fake_dsh = tmp_path / "dsh"
    fake_dsh.write_text("#!/bin/sh\necho 'fatal: no dsh' >&2\nexit 7\n", encoding="utf-8")
    fake_dsh.chmod(0o755)
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><title>desk</title>", encoding="utf-8")
    data_dir = tmp_path / "data"
    port = _free_port()
    dsh_port = _free_port()
    env = os.environ.copy()
    env["TALARIA_DSH_BIN"] = str(fake_dsh)
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys; from talaria.cli.main import main; sys.exit(main(sys.argv[1:]))",
            "up",
            "--port",
            str(port),
            "--dsh-port",
            str(dsh_port),
            "--data-dir",
            str(data_dir),
            "--dist-dir",
            str(dist),
            "--fixture-dir",
            "tests/fixtures/bybit",
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        _wait_for(lambda: _get(f"http://127.0.0.1:{port}/api/health")[0] == 200)

        def offline_payload() -> dict | None:
            payload = json.loads(
                _get(f"http://127.0.0.1:{port}/api/collaboration")[1] or b"{}"
            )
            return payload if payload.get("host") == "offline" else None

        collaboration = _wait_for(offline_payload)
        assert collaboration["mode"] == "dual"
        assert "启动失败" in collaboration["detail"]

        assert _get(f"http://127.0.0.1:{port}/desk")[0] == 200

        descriptor = data_dir / "runtime" / "dsh-bridge.json"
        token = _wait_for(
            lambda: json.loads(descriptor.read_text(encoding="utf-8"))["token"]
            if descriptor.is_file()
            else None
        )
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/snapshots",
            data=json.dumps(
                {
                    "instrument_id": "BTC-USDT-PERP",
                    "timeframes": ["1m"],
                    "mode": "HISTORICAL_PIT",
                }
            ).encode(),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5.0) as response:
            snapshot = json.loads(response.read())
        assert response.status == 200
        assert snapshot["snapshot_id"]
    finally:
        process.send_signal(signal.SIGTERM)
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover - 清理兜底
            process.kill()
            process.wait(timeout=5)
    output = process.stdout.read() if process.stdout is not None else ""
    assert "Talaria 看盘" in output
    assert "DSH 协同宿主：离线" in output


def test_up_dual_keeps_fast_path_when_dsh_binary_missing(tmp_path: Path) -> None:
    """DSH 可执行文件缺失时，协同状态必须是 offline 而不是一直 starting。"""
    data_dir = tmp_path / "data"
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><title>desk</title>", encoding="utf-8")
    port = _free_port()
    dsh_port = _free_port()
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys; from talaria.cli.main import main; sys.exit(main(sys.argv[1:]))",
            "up",
            "--port",
            str(port),
            "--dsh-port",
            str(dsh_port),
            "--dsh-bin",
            str(tmp_path / "does-not-exist-dsh"),
            "--data-dir",
            str(data_dir),
            "--dist-dir",
            str(dist),
            "--fixture-dir",
            "tests/fixtures/bybit",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        _wait_for(lambda: _get(f"http://127.0.0.1:{port}/api/health")[0] == 200)

        def offline_payload() -> dict | None:
            payload = json.loads(
                _get(f"http://127.0.0.1:{port}/api/collaboration")[1] or b"{}"
            )
            return payload if payload.get("host") == "offline" else None

        collaboration = _wait_for(offline_payload)
        assert "不存在" in collaboration["detail"] or "未找到" in collaboration["detail"]
        assert _get(f"http://127.0.0.1:{port}/desk")[0] == 200
    finally:
        process.send_signal(signal.SIGTERM)
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover - 清理兜底
            process.kill()
            process.wait(timeout=5)
    output = process.stdout.read() if process.stdout is not None else ""
    assert "DSH 协同宿主：离线" in output


@pytest.mark.spec
def test_solo_service_publishes_private_bridge_credentials_until_shutdown(tmp_path, monkeypatch):
    # platform/04: coordinate the fake Uvicorn server with the real CLI lifecycle.
    bridge = bridge_descriptor_path(tmp_path)

    def run_server(server):
        server.started = True
        _wait_for(bridge.is_file, timeout=1)
        descriptor = read_service_descriptor(tmp_path)
        assert json.loads(bridge.read_text()) == {
            "version": 1,
            "base_url": descriptor.base_url,
            "token": descriptor.token,
            "daemon_pid": descriptor.pid,
            "created_at": json.loads(bridge.read_text())["created_at"],
        }
        assert bridge.stat().st_mode & 0o777 == 0o600

    monkeypatch.setattr(uvicorn.Server, "run", run_server)
    assert main(["up", "--solo", "--data-dir", str(tmp_path)]) == 0
    assert not bridge.exists()


@pytest.mark.spec
def test_solo_service_finds_desktop_collaboration_after_it_starts(tmp_path, monkeypatch):
    desktop = tmp_path / "runtime/dsh-desktop.json"
    base_url = "http://127.0.0.1:32190"

    def run_server(server):
        server.started = True
        client = TestClient(server.config.app)
        assert client.get("/api/collaboration").json()["host"] != "online"
        desktop.parent.mkdir(exist_ok=True)
        desktop.write_text(json.dumps({"base_url": base_url, "pid": os.getpid()}))
        online = client.get("/api/collaboration").json()
        assert online["host"] == "online"
        assert online["url"] == base_url
        desktop.write_text(json.dumps({"base_url": "http://remote.example", "pid": os.getpid()}))
        assert client.get("/api/collaboration").json()["host"] != "online"
        desktop.unlink()

    monkeypatch.setattr(uvicorn.Server, "run", run_server)
    assert main(["up", "--solo", "--data-dir", str(tmp_path)]) == 0
