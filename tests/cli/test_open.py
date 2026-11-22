from __future__ import annotations

import json
import os
import plistlib
from pathlib import Path

import httpx
import pytest

from talaria.cli import main as cli
from talaria.workbench_api.service_config import FeatureToggles
from talaria.workbench_api.service_descriptor import (
    service_descriptor_path,
    write_service_descriptor,
)
from talaria.workbench_api.service_lock import ServiceLock

pytestmark = [pytest.mark.small, pytest.mark.spec]

BASE_URL = "http://127.0.0.1:8765"
DSH_URL = "http://127.0.0.1:8766/?token=example"


@pytest.fixture
def service(tmp_path, monkeypatch):
    # platform/03: keep the real data-directory lock and description, fake only HTTP.
    def unexpected_browser(url):
        pytest.fail("应使用 --print 或注入浏览器")

    monkeypatch.setattr("webbrowser.open", unexpected_browser)
    write_service_descriptor(
        service_descriptor_path(tmp_path), base_url=BASE_URL, token="bridge",
        pid=os.getpid(), features=FeatureToggles(),
    )
    status = {"host": "online", "pid": 123, "log_path": "/tmp/dsh.log", "detail": ""}

    def get(url, **kwargs):
        assert url == f"{BASE_URL}/api/collaboration"
        return httpx.Response(200, request=httpx.Request("GET", url), json=status)

    monkeypatch.setattr(httpx, "get", get)
    entry = tmp_path / "runtime/dsh-entry.json"
    entry.write_text(json.dumps({"url": DSH_URL, "daemon_pid": os.getpid(), "pid": 123}))
    return status, entry


@pytest.mark.parametrize("scenario", ["online", "offline", "starting", "disabled", "stale", "stopped"])
def test_open_prints_target_for_current_service_state(tmp_path, service, capsys, scenario):
    status, entry = service
    args = ["open", "--data-dir", str(tmp_path), "--print"]
    if scenario == "stopped":
        assert cli.main(args) != 0
        output = capsys.readouterr()
        assert not output.out
        assert "talaria service install" in output.err
        assert "talaria up" in output.err
        return

    if scenario == "stale":
        status["pid"] = 456
    else:
        status["host"] = scenario
    status["detail"] = f"诊断 {DSH_URL}"
    with ServiceLock(tmp_path):
        assert cli.main(args) == 0

    output = capsys.readouterr()
    assert "example" not in output.out + output.err
    if scenario == "online":
        assert output.out.strip() == "http://127.0.0.1:8766/?token=***"
        assert output.err == ""
    else:
        assert output.out.strip() == f"{BASE_URL}/desk"
        assert "DSH" in output.err
        assert status["log_path"] in output.err
    assert entry.is_file()


def test_open_passes_original_entry_only_to_browser(tmp_path, service, monkeypatch, capsys):
    opened = []
    monkeypatch.setattr("webbrowser.open", lambda url: opened.append(url) or True)
    monkeypatch.setenv("TALARIA_DATA_DIR", str(tmp_path))
    with ServiceLock(tmp_path):
        assert cli.main(["open"]) == 0
    assert opened == [DSH_URL]
    assert capsys.readouterr() == ("", "")


def test_open_uses_installed_data_dir_from_another_working_directory(
    tmp_path, service, monkeypatch, capsys
):
    home = tmp_path / "home"
    launch_agents = home / "Library/LaunchAgents"
    launch_agents.mkdir(parents=True)
    (launch_agents / "com.talaria.workbench.plist").write_bytes(plistlib.dumps({
        "ProgramArguments": ["/usr/local/bin/talaria", "up", "--data-dir", str(tmp_path)],
    }))
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.delenv("TALARIA_DATA_DIR", raising=False)
    monkeypatch.chdir(home)
    with ServiceLock(tmp_path):
        assert cli.main(["open", "--print"]) == 0
    assert capsys.readouterr().out.strip() == "http://127.0.0.1:8766/?token=***"


def test_service_status_reports_dsh_pid_and_log(tmp_path, service, capsys):
    with ServiceLock(tmp_path):
        assert cli.main([
            "service", "status", "--data-dir", str(tmp_path),
            "--launch-agents-dir", str(tmp_path / "LaunchAgents"),
        ]) == 0
    assert "DSH 协同宿主：online，pid=123，日志：/tmp/dsh.log" in capsys.readouterr().out
