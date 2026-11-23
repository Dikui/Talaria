"""桌面托管服务的启动配置、状态查询与旧作业卸载。"""

from __future__ import annotations

import errno
import os
import plistlib
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from time import monotonic, sleep
from xml.parsers.expat import ExpatError

from talaria.cli.dsh_host import (
    desktop_installed,
    desktop_plugin_installed,
    desktop_running,
    read_desktop_service_config,
)
from talaria.workbench_api.service_config import (
    FeatureToggles,
    ServiceConfigError,
    load_service_config,
    service_config_path,
)
from talaria.workbench_api.service_descriptor import (
    ServiceDescriptorError,
    read_service_descriptor,
)
from talaria.workbench_api.service_lock import ROLE_SERVICE, LockHolder, probe_lock

DEFAULT_DATA_DIR = Path(".talaria")
DEFAULT_SERVICE_LABEL = "com.talaria.workbench"
_BOOTOUT_TIMEOUT = 5.0
_BOOTOUT_POLL_INTERVAL = 0.1
_PROXY_ENV_NAMES = tuple(
    name for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY")
    for name in (key, key.lower())
)

LaunchctlRunner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


class ServiceUninstallError(RuntimeError):
    """launchctl bootout 失败，plist 已保留。"""


@dataclass(frozen=True, slots=True)
class ServiceUninstallResult:
    plist_path: Path
    removed: bool


@dataclass(frozen=True, slots=True)
class ServiceDshStatus:
    installed: bool
    running: bool
    plugin_installed: bool
    service_configured: bool

    @property
    def summary(self) -> str:
        installed = "已安装" if self.installed else "未安装"
        running = "运行中" if self.running else "未运行"
        plugin = "已安装" if self.plugin_installed else "未安装"
        service = "已配置服务托管" if self.service_configured else "未配置服务托管"
        return f"DSH 桌面应用：{installed}，{running}，Talaria 插件（desktop）：{plugin}，{service}"


@dataclass(frozen=True, slots=True)
class ServiceStatus:
    config_path: Path
    config_exists: bool
    features: FeatureToggles | None
    disk_features: FeatureToggles | None
    lock_holder: LockHolder | None
    endpoint: str | None
    detail: str | None
    dsh: ServiceDshStatus


def plist_path(launch_agents_dir: str | Path, label: str = DEFAULT_SERVICE_LABEL) -> Path:
    return Path(launch_agents_dir).expanduser() / f"{label}.plist"


def legacy_service_warning(path: Path) -> str:
    return f"检测到旧的 launchd 作业：{path}。请运行 talaria service uninstall。"


def service_launch_config(data_dir: Path, port: int, pack: str, program: str) -> dict[str, object]:
    if not 1 <= port <= 65535:
        raise ValueError(f"端口必须在 1..65535 之间：{port}")
    if not Path(program).is_absolute():
        raise ValueError(f"talaria 可执行文件必须是绝对路径：{program}")

    return {
        "command": [
            program, "up", "--solo", "--host", "127.0.0.1", "--port", str(port),
            "--data-dir", str(data_dir), "--pack", pack, "--exit-with-parent",
        ],
        "cwd": str(data_dir),
        "env": {name: os.environ[name] for name in _PROXY_ENV_NAMES if name in os.environ},
        "logDir": str(data_dir / "logs"),
    }


def _desktop_data_dir(service: dict[str, object] | None) -> Path | None:
    if service is None:
        return None
    cwd = service.get("cwd")
    working_dir = cwd if isinstance(cwd, str) and Path(cwd).is_absolute() else None
    arguments = service.get("command")
    if isinstance(arguments, list) and all(isinstance(arg, str) for arg in arguments):
        configured = _job_data_dir(arguments, working_dir)
        if configured is not None:
            return configured
    return Path(working_dir) if working_dir is not None else None


def _legacy_data_dir(target: Path) -> Path | None:
    try:
        config = _read_job_plist(target)
        return _plist_data_dir(config, target)
    except ValueError:
        return None


def open_data_dir(data_dir: str | None) -> Path:
    selected: str | Path | None = data_dir or os.environ.get("TALARIA_DATA_DIR")
    if not selected:
        target = plist_path(Path.home() / "Library" / "LaunchAgents")
        selected = _desktop_data_dir(read_desktop_service_config()) or _legacy_data_dir(target)
    return Path(selected or DEFAULT_DATA_DIR).expanduser().resolve()


def protected_service_data_dirs() -> list[Path]:
    """保护桌面插件以及尚未迁移的旧作业所用的数据目录。"""
    directories = []
    try:
        desktop = _desktop_data_dir(read_desktop_service_config())
        if desktop is not None:
            directories.append(desktop.resolve())
    except (OSError, RuntimeError, ValueError):
        pass

    try:
        result = _run_launchctl(["print", f"{_domain(None)}/{DEFAULT_SERVICE_LABEL}"])
        if result.returncode == 0:
            active = _job_data_dir(
                _launchctl_arguments(result.stdout),
                _launchctl_value(result.stdout, "working directory"),
            )
            if active is not None:
                directories.append(active.resolve())
    except (OSError, RuntimeError, ValueError):
        pass

    try:
        target = plist_path(Path.home() / "Library" / "LaunchAgents")
        config = _read_job_plist(target)
        installed = _plist_data_dir(config, target)
        if installed is None:
            env = config.get("EnvironmentVariables")
            configured = env.get("TALARIA_DATA_DIR") if isinstance(env, dict) else None
            value = configured if isinstance(configured, str) else str(DEFAULT_DATA_DIR)
            working_dir = config.get("WorkingDirectory")
            root = Path(working_dir if isinstance(working_dir, str) else "/")
            installed = root / Path(value).expanduser()
        directories.append(installed.resolve())
    except (OSError, RuntimeError, ValueError):
        pass
    return directories


def uninstall_service(
    *,
    launch_agents_dir: str | Path,
    label: str = DEFAULT_SERVICE_LABEL,
    runner: LaunchctlRunner | None = None,
    uid: int | None = None,
) -> ServiceUninstallResult:
    target = plist_path(launch_agents_dir, label)
    if not target.exists():
        return ServiceUninstallResult(plist_path=target, removed=False)
    launchctl = runner or _run_launchctl
    job = f"{_domain(uid)}/{label}"
    result = launchctl(["bootout", job])
    if result.returncode not in (0, errno.ESRCH):
        detail = (result.stderr or result.stdout).strip() or "无输出"
        raise ServiceUninstallError(
            f"launchctl bootout 失败（退出码 {result.returncode}）：{detail}\n"
            f"plist 已保留：{target}"
        )
    if not _wait_for_job_exit(launchctl, job):
        raise ServiceUninstallError(f"等待 launchd 作业退出超时：{job}\nplist 已保留：{target}")
    target.unlink()
    return ServiceUninstallResult(plist_path=target, removed=True)


def service_status(
    *,
    launch_agents_dir: str | Path,
    data_dir: str | Path | None = None,
    label: str = DEFAULT_SERVICE_LABEL,
) -> ServiceStatus:
    target = plist_path(launch_agents_dir, label)
    service = read_desktop_service_config()
    selected = (
        data_dir or _desktop_data_dir(service) or _legacy_data_dir(target)
        or os.environ.get("TALARIA_DATA_DIR") or DEFAULT_DATA_DIR
    )
    resolved_data_dir = Path(selected).expanduser().resolve()
    holder = probe_lock(resolved_data_dir)
    endpoint: str | None = None
    detail: str | None = None
    features: FeatureToggles | None = None
    if holder is not None and holder.role == ROLE_SERVICE:
        try:
            descriptor = read_service_descriptor(resolved_data_dir)
        except ServiceDescriptorError as exc:
            detail = str(exc)
        else:
            if descriptor is not None and holder.pid == descriptor.pid:
                endpoint = descriptor.base_url
                features = descriptor.features
            elif descriptor is not None:
                detail = f"锁持有者 pid={holder.pid} 与接口描述 pid={descriptor.pid} 不一致"

    config = None
    try:
        config = load_service_config(resolved_data_dir)
    except ServiceConfigError as exc:
        if features is None:
            raise
        detail = str(exc)

    installed_desktop = desktop_installed()
    return ServiceStatus(
        config_path=service_config_path(resolved_data_dir),
        config_exists=config is None or config.exists,
        features=features,
        disk_features=config.features if config is not None else None,
        lock_holder=holder,
        endpoint=endpoint,
        detail=detail,
        dsh=ServiceDshStatus(
            installed_desktop,
            installed_desktop and desktop_running(),
            service is not None or desktop_plugin_installed(),
            service is not None,
        ),
    )


def _wait_for_job_exit(launchctl: LaunchctlRunner, job: str) -> bool:
    deadline = monotonic() + _BOOTOUT_TIMEOUT
    while launchctl(["print", job]).returncode == 0:
        remaining = deadline - monotonic()
        if remaining <= 0:
            return False
        sleep(min(_BOOTOUT_POLL_INTERVAL, remaining))
    return True


def _run_launchctl(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["launchctl", *args],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return subprocess.CompletedProcess(
            ["launchctl", *args], 127, "", "未找到 launchctl 命令（仅 macOS 支持）"
        )


def _domain(uid: int | None) -> str:
    return f"gui/{os.getuid() if uid is None else uid}"


def _launchctl_value(stdout: str, key: str) -> str | None:
    for line in stdout.splitlines():
        left, separator, right = line.strip().partition("=")
        if separator and left.strip() == key:
            return right.strip()
    return None


def _launchctl_arguments(stdout: str) -> list[str]:
    lines = iter(stdout.splitlines())
    for line in lines:
        if line.strip() == "arguments = {":
            arguments = []
            for argument in lines:
                if argument.strip() == "}":
                    return arguments
                arguments.append(argument.strip())
            break
    return []


def _job_data_dir(arguments: Sequence[str], working_dir: str | None) -> Path | None:
    for index in range(len(arguments) - 1, -1, -1):
        option, separator, value = arguments[index].partition("=")
        if option != "--data-dir":
            continue
        if not separator and index + 1 < len(arguments):
            value = arguments[index + 1]
        if value:
            return Path(working_dir or "/") / Path(value).expanduser()
    return None


def _read_job_plist(path: Path) -> dict[str, object]:
    try:
        with path.open("rb") as handle:
            plist = plistlib.load(handle)
        if not isinstance(plist, dict):
            raise ValueError("顶层必须是字典")
    except (OSError, ValueError, plistlib.InvalidFileException, ExpatError) as exc:
        raise ValueError(f"无法读取作业 plist {path}：{exc}") from exc
    return plist


def _plist_data_dir(plist: dict[str, object], path: Path) -> Path | None:
    arguments = plist.get("ProgramArguments", [])
    if not isinstance(arguments, list) or not all(isinstance(arg, str) for arg in arguments):
        raise ValueError(f"无法读取作业 plist {path}：ProgramArguments 必须是字符串数组")
    working_dir = plist.get("WorkingDirectory")
    return _job_data_dir(arguments, working_dir if isinstance(working_dir, str) else None)
