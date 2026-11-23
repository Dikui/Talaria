from __future__ import annotations

import os
import plistlib
import subprocess
from pathlib import Path

import pytest

from talaria.cli import dsh_host
from talaria.cli import service as service_module
from talaria.cli.main import main
from talaria.cli.service import (
    DEFAULT_SERVICE_LABEL,
    ServiceUninstallError,
    uninstall_service,
)
from talaria.workbench_api.service_config import (
    FeatureToggles,
    service_config_path,
)
from talaria.workbench_api.service_descriptor import (
    service_descriptor_path,
    write_service_descriptor,
)
from talaria.workbench_api.service_lock import ServiceLock

pytestmark = pytest.mark.small

@pytest.fixture(autouse=True)
def isolate_desktop(tmp_path, monkeypatch):
    monkeypatch.setenv("DSH_HOME", str(tmp_path / "dsh-home"))
    monkeypatch.setattr(dsh_host, "DESKTOP_APP", tmp_path / "DeepSeek Harness.app")
    monkeypatch.delenv("TALARIA_DATA_DIR", raising=False)


class FakeRunner:
    def __init__(self, results: dict[str, subprocess.CompletedProcess | int] | None = None):
        self.calls: list[list[str]] = []
        self.results = results or {}

    def __call__(self, args: list[str]) -> subprocess.CompletedProcess:
        self.calls.append(list(args))
        result = self.results.get(args[0], 113 if args[0] == "print" else 0)
        if isinstance(result, subprocess.CompletedProcess):
            return result
        return subprocess.CompletedProcess(args, result, "", "")


def _completed(args: list[str], returncode: int, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess(args, returncode, stdout, stderr)


def _legacy_job(launch_agents: Path) -> Path:
    launch_agents.mkdir(parents=True)
    path = launch_agents / f"{DEFAULT_SERVICE_LABEL}.plist"
    path.write_bytes(plistlib.dumps({"Label": DEFAULT_SERVICE_LABEL}))
    return path


def test_uninstall_boots_out_and_removes_plist_idempotently(tmp_path):
    launch_agents = tmp_path / "LaunchAgents"
    plist = _legacy_job(launch_agents)
    runner = FakeRunner()

    first = uninstall_service(
        launch_agents_dir=tmp_path / "LaunchAgents",
        label=DEFAULT_SERVICE_LABEL,
        runner=runner,
        uid=501,
    )

    assert first.removed is True
    assert not plist.exists()
    assert runner.calls == [
        ["bootout", f"gui/501/{DEFAULT_SERVICE_LABEL}"],
        ["print", f"gui/501/{DEFAULT_SERVICE_LABEL}"],
    ]

    runner.calls.clear()
    second = uninstall_service(
        launch_agents_dir=tmp_path / "LaunchAgents",
        label=DEFAULT_SERVICE_LABEL,
        runner=runner,
        uid=501,
    )
    assert second.removed is False
    assert runner.calls == []


@pytest.mark.regression
def test_uninstall_times_out_and_keeps_plist_when_job_still_exists(tmp_path, monkeypatch):
    # platform/04: the runner keeps the job registered and the fake clock avoids a five-second wait.
    launch_agents = tmp_path / "LaunchAgents"
    plist = launch_agents / f"{DEFAULT_SERVICE_LABEL}.plist"
    launch_agents.mkdir()
    plist.touch()
    elapsed = 0.0

    def sleep(seconds):
        nonlocal elapsed
        elapsed += seconds

    monkeypatch.setattr(service_module, "monotonic", lambda: elapsed)
    monkeypatch.setattr(service_module, "sleep", sleep)
    runner = FakeRunner({"print": 0})
    with pytest.raises(ServiceUninstallError, match="等待 launchd 作业退出超时"):
        uninstall_service(launch_agents_dir=launch_agents, runner=runner, uid=501)

    assert elapsed == pytest.approx(5)
    assert plist.is_file()


@pytest.mark.regression
@pytest.mark.parametrize(
    ("returncode", "diagnostic", "expected_exit"),
    [
        (3, "No such process", 0),
        (5, "Input/output error", 2),
        (1, "Operation not permitted", 2),
    ],
)
def test_uninstall_cli_preserves_plist_on_bootout_failure(
    tmp_path, monkeypatch, capsys, returncode, diagnostic, expected_exit
):
    # platform/01: bootout 失败时保留作业定义以便重试。
    launch_agents = tmp_path / "LaunchAgents"
    plist = _legacy_job(launch_agents)
    runner = FakeRunner({"bootout": _completed(["bootout"], returncode, stderr=diagnostic)})
    monkeypatch.setattr(service_module, "_run_launchctl", runner)

    code = main([
        "service", "uninstall", "--data-dir", str(tmp_path / "data"),
        "--launch-agents-dir", str(launch_agents),
    ])

    assert code == expected_exit
    output = capsys.readouterr()
    if expected_exit:
        assert plist.is_file()
        assert diagnostic in output.err
        assert str(plist) in output.err
        assert "已卸载" not in output.out
    else:
        assert not plist.exists()
        assert "已卸载" in output.out


# platform/05: 迁移提示只检测 plist，状态查询不会调用 launchctl。
@pytest.mark.spec
@pytest.mark.parametrize("legacy", [False, True])
def test_status_reports_legacy_plist_for_migration(tmp_path, monkeypatch, capsys, legacy):
    launch_agents = tmp_path / "LaunchAgents"
    if legacy:
        plist = _legacy_job(launch_agents)
    monkeypatch.setattr(
        service_module, "_run_launchctl", lambda args: pytest.fail("status 调用了 launchctl")
    )

    assert main([
        "service", "status", "--launch-agents-dir", str(launch_agents),
        "--data-dir", str(tmp_path / "data"),
    ]) == 0

    output = capsys.readouterr().out
    assert ("talaria service uninstall" in output) is legacy
    if legacy:
        assert str(plist) in output
        assert plist.is_file()
    assert "数据目录锁：空闲" in output
    assert "写入接口：不可用" in output
    assert "磁盘配置：行情采集=开；冷库写入=开；预警=开；扫描=开" in output
    assert "launchd=" not in output


@pytest.mark.regression
@pytest.mark.parametrize("descriptor_matches", [True, False])
def test_status_cli_reports_matching_runtime_and_pending_changes(
    tmp_path, monkeypatch, capsys, descriptor_matches
):
    # platform/05: 从桌面插件配置读取数据目录，检查磁盘配置与运行态差异。
    launch_agents = tmp_path / "LaunchAgents"
    data_dir = tmp_path / "active-service"
    monkeypatch.setenv("TALARIA_DATA_DIR", str(tmp_path / "other-service"))
    profile = dsh_host.dsh_profile_dir(dsh_host.DESKTOP_PROFILE)
    profile.mkdir(parents=True)
    dsh_host.install_talaria_patch(
        profile, Path(__file__).resolve().parents[2] / "dsh/packages/talaria-dsh-plugin", data_dir,
        service={"cwd": str(data_dir)},
    )
    data_dir.mkdir()
    service_config_path(data_dir).write_text('{"features":{"scanner":false}}')

    with ServiceLock(data_dir):
        write_service_descriptor(
            service_descriptor_path(data_dir),
            base_url="http://127.0.0.1:8765",
            token="test-token",
            pid=os.getpid() if descriptor_matches else os.getpid() + 1,
            features=FeatureToggles(),
        )
        code = main(["service", "status", "--launch-agents-dir", str(launch_agents)])

    assert code == 0
    output = capsys.readouterr().out
    assert f"配置文件：{service_config_path(data_dir)}" in output
    assert f"数据目录锁：由 service 持有（pid={os.getpid()}）" in output
    assert "Talaria 插件（desktop）：已安装，已配置服务托管" in output
    if descriptor_matches:
        assert "运行态功能开关：行情采集=开；冷库写入=开；预警=开；扫描=开" in output
        assert "待重启生效：扫描=关" in output
        assert "写入接口：http://127.0.0.1:8765" in output
    else:
        assert "运行态功能开关：不可用" in output
        assert "磁盘配置：行情采集=开；冷库写入=开；预警=开；扫描=关" in output
        assert "不一致" in output
        assert "待重启生效" not in output
        assert "写入接口：不可用" in output


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


@pytest.mark.parametrize("installed,running,plugin", [
    (False, False, None), (True, False, None), (True, True, "bundle"), (True, True, "patch"),
])
def test_service_status_reports_desktop_and_profile(
    tmp_path, monkeypatch, capsys, installed, running, plugin
):
    app = tmp_path / "DeepSeek Harness.app"
    if installed:
        app.mkdir()
    monkeypatch.setattr(dsh_host, "DESKTOP_APP", app, raising=False)
    monkeypatch.setenv("DSH_HOME", str(tmp_path))
    if plugin == "bundle":
        profile = tmp_path / "profiles/desktop"
        package = profile / "node_modules/@talaria/dsh-plugin/package.json"
        package.parent.mkdir(parents=True)
        package.write_text('{"name":"@talaria/dsh-plugin"}')
        (profile / "package.json").write_text(
            '{"dsh":{"profile":{"bundles":["@talaria/dsh-plugin"]}}}'
        )
    if plugin == "patch":
        profile = tmp_path / "profiles/desktop"
        profile.mkdir(parents=True)
        (profile / "cordis.patch.yml").write_text(
            "# >>> talaria (added by Talaria; remove this block to uninstall)\n"
            "- insert:\n"
            "    - id: talaria-desk-plugin\n"
            "      name: /test/plugin/index.js\n"
            "# <<< talaria\n"
        )

    def pgrep(command, **kwargs):
        assert installed
        assert command[:2] == ["/usr/bin/pgrep", "-f"]
        return _completed(command, 0 if running else 1)

    monkeypatch.setattr(subprocess, "run", pgrep)
    assert main(["service", "status", "--launch-agents-dir", str(tmp_path)]) == 0
    output = capsys.readouterr().out
    assert f"DSH 桌面应用：{'已安装' if installed else '未安装'}" in output
    assert f"，{'运行中' if running else '未运行'}" in output
    assert f"Talaria 插件（desktop）：{'已安装' if plugin else '未安装'}" in output
    assert "未配置服务托管" in output
