from __future__ import annotations

import os
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

from talaria.cli import dsh_host
from talaria.cli import main as cli
from talaria.workbench_api.service_config import FeatureToggles
from talaria.workbench_api.service_descriptor import (
    service_descriptor_path,
    write_service_descriptor,
)
from talaria.workbench_api.service_lock import ServiceLock

pytestmark = [pytest.mark.small, pytest.mark.spec]

BASE_URL = "http://127.0.0.1:8765"
DESKTOP_ACTION = "open -a \"DeepSeek Harness\""


@pytest.fixture
def service(tmp_path, monkeypatch):
    # platform/04: the service lock and descriptor are real, desktop operations are faked.
    def unexpected_open(*args, **kwargs):
        pytest.fail("应使用 --print 或注入启动命令")

    monkeypatch.setattr("webbrowser.open", unexpected_open)
    monkeypatch.setattr("subprocess.run", unexpected_open)
    monkeypatch.setattr(dsh_host, "DESKTOP_APP", tmp_path / "DeepSeek Harness.app", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("DSH_HOME", str(tmp_path / "dsh-home"))
    write_service_descriptor(
        service_descriptor_path(tmp_path), base_url=BASE_URL, token="bridge",
        pid=os.getpid(), features=FeatureToggles(),
    )


# platform/05: 桌面应用可以在服务未运行时直接打开。
@pytest.mark.parametrize("scenario", ["installed", "missing", "stopped"])
def test_open_prints_action_for_service_and_desktop_state(tmp_path, service, capsys, scenario):
    args = ["open", "--data-dir", str(tmp_path), "--print"]
    if scenario == "stopped":
        assert cli.main(args) == 2
        output = capsys.readouterr()
        assert not output.out
        assert "talaria up --solo" in output.err
        return

    if scenario == "installed":
        dsh_host.DESKTOP_APP.mkdir()
    with ServiceLock(tmp_path) if scenario == "missing" else nullcontext():
        assert cli.main(args) == 0
    output = capsys.readouterr()
    if scenario == "installed":
        assert output == (f"{DESKTOP_ACTION}\n", "")
    else:
        assert f"{BASE_URL}/desk" in output.out
        assert "兜底页面" in output.err


@pytest.mark.parametrize("installed", [True, False])
def test_open_dispatches_desktop_action_or_fallback(tmp_path, service, monkeypatch, installed):
    opened = []
    if installed:
        dsh_host.DESKTOP_APP.mkdir()
        monkeypatch.setattr(
            "subprocess.run",
            lambda command, **kwargs: opened.append(command) or SimpleNamespace(returncode=0),
        )
    else:
        monkeypatch.setattr("webbrowser.open", lambda url: opened.append(url) or True)
    monkeypatch.setenv("TALARIA_DATA_DIR", str(tmp_path))
    with nullcontext() if installed else ServiceLock(tmp_path):
        assert cli.main(["open"]) == 0
    assert opened == ([["open", "-a", "DeepSeek Harness"]] if installed else [f"{BASE_URL}/desk"])


@pytest.mark.regression
@pytest.mark.parametrize("publish_after", [3.0, None], ids=["published", "timed-out"])
def test_open_waits_up_to_ten_seconds_for_starting_service(
    tmp_path, service, monkeypatch, capsys, publish_after
):
    # platform/03: keep the actual service lock and simulate the passage of time.
    path = service_descriptor_path(tmp_path)
    contents = path.read_bytes()
    path.unlink()
    elapsed = 0.0

    def sleep(seconds):
        nonlocal elapsed
        assert capsys.readouterr() == ("", "")
        elapsed += seconds
        if publish_after is not None and elapsed >= publish_after and not path.exists():
            path.write_bytes(contents)

    monkeypatch.setattr(cli, "time", SimpleNamespace(monotonic=lambda: elapsed, sleep=sleep))
    with ServiceLock(tmp_path):
        result = cli.main(["open", "--data-dir", str(tmp_path), "--print"])

    output = capsys.readouterr()
    if publish_after is None:
        assert elapsed == pytest.approx(10.0)
        assert result == 2
        assert "常驻服务未就绪" in output.err
    else:
        assert publish_after <= elapsed < 10
        assert result == 0
        assert output.out == f"open {BASE_URL}/desk\n"


def test_open_fallback_uses_configured_data_dir_from_another_working_directory(
    tmp_path, service, monkeypatch, capsys
):
    profile = dsh_host.dsh_profile_dir(dsh_host.DESKTOP_PROFILE)
    profile.mkdir(parents=True)
    dsh_host.install_talaria_patch(
        profile, Path(__file__).resolve().parents[2] / "dsh/packages/talaria-dsh-plugin", tmp_path,
        service={"cwd": str(tmp_path)},
    )
    monkeypatch.delenv("TALARIA_DATA_DIR", raising=False)
    monkeypatch.chdir(profile)
    with ServiceLock(tmp_path):
        assert cli.main(["open", "--print"]) == 0
    assert capsys.readouterr().out.strip() == f"open {BASE_URL}/desk"
