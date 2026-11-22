from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from talaria.cli.main import main
from talaria.cli.service import (
    DEFAULT_SERVICE_LABEL,
    ServiceInstallError,
    install_service,
    render_launchd_plist,
    service_status,
    uninstall_service,
)
from talaria.workbench_api.service_config import load_service_config, service_config_path
from talaria.workbench_api.service_descriptor import (
    service_descriptor_path,
    write_service_descriptor,
)
from talaria.workbench_api.service_lock import ServiceLock

pytestmark = pytest.mark.small

DATA_DIR = Path("/Users/me/Talaria")


class FakeRunner:
    def __init__(self, results: dict[str, subprocess.CompletedProcess | int] | None = None):
        self.calls: list[list[str]] = []
        self.results = results or {}

    def __call__(self, args: list[str]) -> subprocess.CompletedProcess:
        self.calls.append(list(args))
        result = self.results.get(args[0], 0)
        if isinstance(result, subprocess.CompletedProcess):
            return result
        return subprocess.CompletedProcess(args, result, "", "")


def _completed(args: list[str], returncode: int, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess(args, returncode, stdout, stderr)


def test_launchd_plist_renders_expected_snapshot():
    text = render_launchd_plist(
        label=DEFAULT_SERVICE_LABEL,
        program_arguments=[
            "/opt/talaria/bin/talaria",
            "up",
            "--solo",
            "--host",
            "127.0.0.1",
            "--port",
            "8765",
            "--data-dir",
            str(DATA_DIR),
            "--pack",
            "crypto_perp",
        ],
        data_dir=DATA_DIR,
        stdout_path=DATA_DIR / "logs" / "service.out.log",
        stderr_path=DATA_DIR / "logs" / "service.err.log",
        working_directory=DATA_DIR,
    )

    assert text == """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.talaria.workbench</string>
  <key>ProgramArguments</key>
  <array>
    <string>/opt/talaria/bin/talaria</string>
    <string>up</string>
    <string>--solo</string>
    <string>--host</string>
    <string>127.0.0.1</string>
    <string>--port</string>
    <string>8765</string>
    <string>--data-dir</string>
    <string>/Users/me/Talaria</string>
    <string>--pack</string>
    <string>crypto_perp</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <dict>
    <key>SuccessfulExit</key>
    <false/>
  </dict>
  <key>ProcessType</key>
  <string>Interactive</string>
  <key>ThrottleInterval</key>
  <integer>10</integer>
  <key>WorkingDirectory</key>
  <string>/Users/me/Talaria</string>
  <key>StandardOutPath</key>
  <string>/Users/me/Talaria/logs/service.out.log</string>
  <key>StandardErrorPath</key>
  <string>/Users/me/Talaria/logs/service.err.log</string>
</dict>
</plist>
"""


def test_launchd_plist_escapes_xml_special_characters():
    text = render_launchd_plist(
        label="com.talaria.workbench",
        program_arguments=["/tmp/a&b/<talaria>"],
        data_dir=Path("/tmp/数据 & 回放"),
        stdout_path=Path("/tmp/out.log"),
        stderr_path=Path("/tmp/err.log"),
    )

    assert "/tmp/a&amp;b/&lt;talaria&gt;" in text
    assert "/tmp/数据 &amp; 回放" in text


def test_install_writes_config_plist_logs_and_bootstraps(tmp_path):
    data_dir = tmp_path / "data"
    launch_agents = tmp_path / "LaunchAgents"
    runner = FakeRunner(
        {
            "bootout": _completed(["bootout"], 3, "", "Boot-out failed: 3: No such process"),
            "bootstrap": 0,
        }
    )

    result = install_service(
        data_dir=data_dir,
        port=8765,
        pack="crypto_perp",
        program="/opt/talaria/bin/talaria",
        launch_agents_dir=launch_agents,
        label=DEFAULT_SERVICE_LABEL,
        runner=runner,
        uid=501,
    )

    assert result.plist_path == launch_agents / f"{DEFAULT_SERVICE_LABEL}.plist"
    assert result.config_path == data_dir / "service.json"
    assert result.stdout_path == data_dir / "logs" / "service.out.log"
    assert result.stderr_path == data_dir / "logs" / "service.err.log"
    assert result.bootstrapped is True
    assert result.features == load_service_config(data_dir).features
    assert result.config_path.is_file()
    assert result.stdout_path.parent.is_dir()
    assert result.plist_path.is_file()
    assert "KeepAlive" in result.plist_path.read_text(encoding="utf-8")
    assert "ProcessType" in result.plist_path.read_text(encoding="utf-8")
    assert runner.calls == [
        ["bootout", f"gui/501/{DEFAULT_SERVICE_LABEL}"],
        ["bootstrap", "gui/501", str(result.plist_path)],
    ]


def test_install_keeps_existing_feature_config(tmp_path):
    data_dir = tmp_path / "data"
    path = service_config_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"features": {"scanner": False}}), encoding="utf-8")
    runner = FakeRunner()

    install_service(
        data_dir=data_dir,
        port=8765,
        pack="crypto_perp",
        program="/opt/talaria/bin/talaria",
        launch_agents_dir=tmp_path / "LaunchAgents",
        runner=runner,
        uid=501,
    )

    assert json.loads(path.read_text(encoding="utf-8"))["features"] == {"scanner": False}


def test_install_reports_bootstrap_failure(tmp_path):
    runner = FakeRunner(
        {"bootstrap": _completed(["bootstrap"], 5, "", "Bootstrap failed: 5: Input/output error")}
    )

    with pytest.raises(ServiceInstallError) as excinfo:
        install_service(
            data_dir=tmp_path / "data",
            port=8765,
            pack="crypto_perp",
            program="/opt/talaria/bin/talaria",
            launch_agents_dir=tmp_path / "LaunchAgents",
            runner=runner,
            uid=501,
        )

    assert "Bootstrap failed" in str(excinfo.value)


def test_uninstall_boots_out_and_removes_plist_idempotently(tmp_path):
    data_dir = tmp_path / "data"
    runner = FakeRunner()
    install_service(
        data_dir=data_dir,
        port=8765,
        pack="crypto_perp",
        program="/opt/talaria/bin/talaria",
        launch_agents_dir=tmp_path / "LaunchAgents",
        runner=runner,
        uid=501,
    )
    plist = tmp_path / "LaunchAgents" / f"{DEFAULT_SERVICE_LABEL}.plist"
    runner.calls.clear()

    first = uninstall_service(
        launch_agents_dir=tmp_path / "LaunchAgents",
        label=DEFAULT_SERVICE_LABEL,
        runner=runner,
        uid=501,
    )

    assert first.removed is True
    assert not plist.exists()
    assert runner.calls == [["bootout", f"gui/501/{DEFAULT_SERVICE_LABEL}"]]

    runner.calls.clear()
    second = uninstall_service(
        launch_agents_dir=tmp_path / "LaunchAgents",
        label=DEFAULT_SERVICE_LABEL,
        runner=runner,
        uid=501,
    )
    assert second.removed is False
    assert runner.calls == []


def test_status_reports_running_service_and_features(tmp_path):
    data_dir = tmp_path / "data"
    launch_agents = tmp_path / "LaunchAgents"
    install_service(
        data_dir=data_dir,
        port=8765,
        pack="crypto_perp",
        program="/opt/talaria/bin/talaria",
        launch_agents_dir=launch_agents,
        runner=FakeRunner(),
        uid=501,
    )
    service_config_path(data_dir).write_text(
        json.dumps({"features": {"scanner": False}}), encoding="utf-8"
    )
    runner = FakeRunner(
        {
            "print": _completed(
                ["print"],
                0,
                "gui/501/com.talaria.workbench = {\n\tstate = running\n\tpid = 4321\n}\n",
            )
        }
    )
    lock = ServiceLock(data_dir, role="service")
    lock.acquire()
    try:
        write_service_descriptor(
            service_descriptor_path(data_dir),
            base_url="http://127.0.0.1:8765",
            token="test-token",
            pid=os.getpid(),
            features=load_service_config(data_dir).features,
        )
        status = service_status(
            data_dir=data_dir,
            launch_agents_dir=launch_agents,
            label=DEFAULT_SERVICE_LABEL,
            runner=runner,
            uid=501,
        )
    finally:
        lock.release()

    assert status.installed is True
    assert status.loaded is True
    assert status.state == "running"
    assert status.pid == 4321
    assert status.features.scanner is False
    assert status.config_exists is True
    assert status.lock_holder is not None
    assert status.lock_holder.pid == os.getpid()
    assert status.endpoint == "http://127.0.0.1:8765"
    assert runner.calls == [["print", f"gui/501/{DEFAULT_SERVICE_LABEL}"]]


def test_status_when_not_installed_does_not_invoke_launchctl(tmp_path):
    runner = FakeRunner()

    status = service_status(
        data_dir=tmp_path / "data",
        launch_agents_dir=tmp_path / "LaunchAgents",
        label=DEFAULT_SERVICE_LABEL,
        runner=runner,
        uid=501,
    )

    assert status.installed is False
    assert status.loaded is False
    assert status.features == load_service_config(tmp_path / "data").features
    assert runner.calls == []


def test_status_reports_not_loaded_when_launchctl_print_fails(tmp_path):
    data_dir = tmp_path / "data"
    launch_agents = tmp_path / "LaunchAgents"
    install_service(
        data_dir=data_dir,
        port=8765,
        pack="crypto_perp",
        program="/opt/talaria/bin/talaria",
        launch_agents_dir=launch_agents,
        runner=FakeRunner(),
        uid=501,
    )
    runner = FakeRunner(
        {"print": _completed(["print"], 113, "", "Could not find service")}
    )

    status = service_status(
        data_dir=data_dir,
        launch_agents_dir=launch_agents,
        label=DEFAULT_SERVICE_LABEL,
        runner=runner,
        uid=501,
    )

    assert status.installed is True
    assert status.loaded is False
    assert status.pid is None
    assert "Could not find service" in (status.detail or "")


def test_service_status_cli_reports_features_without_launchctl(tmp_path, capsys):
    code = main(
        [
            "service",
            "status",
            "--data-dir",
            str(tmp_path / "data"),
            "--launch-agents-dir",
            str(tmp_path / "LaunchAgents"),
        ]
    )

    output = capsys.readouterr().out
    assert code == 0
    assert "未安装" in output
    assert "行情采集=开" in output
    assert "冷库写入=开" in output
    assert "预警=开" in output
    assert "扫描=开" in output


def test_up_rejects_invalid_service_config_before_starting(tmp_path, capsys):
    data_dir = tmp_path / "data"
    path = service_config_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{bad json", encoding="utf-8")

    code = main(["up", "--solo", "--data-dir", str(data_dir), "--port", "18765"])

    captured = capsys.readouterr()
    assert code == 2
    assert "service.json" in captured.err
    assert "JSON" in captured.err
