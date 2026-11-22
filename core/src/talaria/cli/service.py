"""launchd 用户级 Agent 的生成、安装与状态查询。"""

from __future__ import annotations

import errno
import json
import os
import plistlib
import subprocess
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from time import monotonic, sleep
from urllib.parse import urlsplit
from xml.parsers.expat import ExpatError
from xml.sax.saxutils import escape

from talaria.cli.dsh_host import (
    desktop_installed,
    desktop_plugin_installed,
    desktop_running,
)
from talaria.workbench_api.service_config import (
    FeatureToggles,
    ServiceConfigError,
    describe_features,
    load_service_config,
    service_config_path,
    write_default_service_config,
)
from talaria.workbench_api.service_descriptor import (
    ServiceDescriptorError,
    read_service_descriptor,
)
from talaria.workbench_api.service_lock import ROLE_SERVICE, LockHolder, probe_lock

DEFAULT_DATA_DIR = Path(".talaria")
DEFAULT_SERVICE_LABEL = "com.talaria.workbench"
SERVICE_LOG_SUBDIR = Path("logs")
SERVICE_STDOUT_NAME = "service.out.log"
SERVICE_STDERR_NAME = "service.err.log"
_BOOTOUT_TIMEOUT = 5.0
_BOOTOUT_POLL_INTERVAL = 0.1
_BOOTSTRAP_RETRY_DELAY = 0.2
_PROXY_VARIABLES = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")
_PROXY_ENV_NAMES = tuple(
    name for key in (*_PROXY_VARIABLES, "NO_PROXY") for name in (key, key.lower())
)

LaunchctlRunner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


class ServiceInstallError(RuntimeError):
    """launchd 注册失败。"""


class ServiceUninstallError(RuntimeError):
    """``launchctl bootout`` 失败，plist 已保留。"""


@dataclass(frozen=True, slots=True)
class ServiceInstallResult:
    plist_path: Path
    config_path: Path
    stdout_path: Path
    stderr_path: Path
    bootstrapped: bool
    features: FeatureToggles


@dataclass(frozen=True, slots=True)
class ServiceUninstallResult:
    plist_path: Path
    removed: bool


@dataclass(frozen=True, slots=True)
class ServiceDshStatus:
    installed: bool
    running: bool
    plugin_installed: bool

    @property
    def summary(self) -> str:
        installed = "已安装" if self.installed else "未安装"
        running = "运行中" if self.running else "未运行"
        plugin = "已安装" if self.plugin_installed else "未安装"
        return f"DSH 桌面应用：{installed}，{running}，Talaria 插件（desktop）：{plugin}"


@dataclass(frozen=True, slots=True)
class ServiceStatus:
    plist_path: Path
    config_path: Path
    installed: bool
    loaded: bool
    config_exists: bool
    features: FeatureToggles | None
    disk_features: FeatureToggles | None
    state: str | None
    pid: int | None
    lock_holder: LockHolder | None
    endpoint: str | None
    detail: str | None
    dsh: ServiceDshStatus
    proxy_endpoints: tuple[str, ...] = ()


def plist_path(launch_agents_dir: str | Path, label: str = DEFAULT_SERVICE_LABEL) -> Path:
    return Path(launch_agents_dir).expanduser() / f"{label}.plist"


def open_data_dir(data_dir: str | None) -> Path:
    selected: str | Path | None = data_dir or os.environ.get("TALARIA_DATA_DIR")
    if selected is None:
        job = plist_path(Path.home() / "Library" / "LaunchAgents")
        if job.is_file():
            selected = _plist_data_dir(_read_job_plist(job), job)
    return Path(selected or DEFAULT_DATA_DIR).expanduser().resolve()


def render_launchd_plist(
    *,
    label: str,
    program_arguments: Sequence[str],
    data_dir: str | Path,
    stdout_path: str | Path,
    stderr_path: str | Path,
    working_directory: str | Path | None = None,
    process_type: str = "Interactive",
    throttle_interval: int = 10,
    environment: Mapping[str, str] | None = None,
) -> str:
    """纯函数：同样的输入永远生成同样的 plist，便于快照测试与审计。"""

    if not label:
        raise ValueError("launchd Label 不能为空")
    if not program_arguments:
        raise ValueError("launchd ProgramArguments 不能为空")
    if throttle_interval < 1:
        raise ValueError("ThrottleInterval 必须为正整数")
    workdir = Path(working_directory) if working_directory is not None else Path(data_dir)
    arguments = "\n".join(
        f"    <string>{escape(str(argument))}</string>" for argument in program_arguments
    )
    environment_xml = ""
    if environment:
        variables = "".join(
            f"    <key>{escape(name)}</key>\n    <string>{escape(value)}</string>\n"
            for name, value in environment.items()
        )
        environment_xml = f"  <key>EnvironmentVariables</key>\n  <dict>\n{variables}  </dict>\n"
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>{escape(label)}</string>
  <key>ProgramArguments</key>
  <array>
{arguments}
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <dict>
    <key>SuccessfulExit</key>
    <false/>
  </dict>
  <key>ProcessType</key>
  <string>{escape(process_type)}</string>
  <key>ThrottleInterval</key>
  <integer>{throttle_interval}</integer>
  <key>WorkingDirectory</key>
  <string>{escape(str(workdir))}</string>
  <key>StandardOutPath</key>
  <string>{escape(str(stdout_path))}</string>
  <key>StandardErrorPath</key>
  <string>{escape(str(stderr_path))}</string>
{environment_xml}</dict>
</plist>
"""


def install_service(
    *,
    data_dir: str | Path,
    port: int,
    pack: str,
    program: str,
    launch_agents_dir: str | Path,
    log_dir: str | Path | None = None,
    label: str = DEFAULT_SERVICE_LABEL,
    runner: LaunchctlRunner | None = None,
    uid: int | None = None,
) -> ServiceInstallResult:
    resolved_data_dir = Path(data_dir).expanduser().resolve()
    if not 1 <= port <= 65535:
        raise ValueError(f"端口必须在 1..65535 之间：{port}")
    if not Path(program).is_absolute():
        raise ValueError(f"launchd 的 program 必须是绝对路径：{program}")

    config_path = write_default_service_config(resolved_data_dir)
    features = load_service_config(resolved_data_dir).features

    log_root = (
        Path(log_dir).expanduser().resolve()
        if log_dir is not None
        else resolved_data_dir / SERVICE_LOG_SUBDIR
    )
    log_root.mkdir(parents=True, exist_ok=True)
    stdout_path = log_root / SERVICE_STDOUT_NAME
    stderr_path = log_root / SERVICE_STDERR_NAME

    arguments = [
        program,
        "up",
        "--solo",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--data-dir",
        str(resolved_data_dir),
        "--pack",
        pack,
    ]
    proxy_env = {name: os.environ[name] for name in _PROXY_ENV_NAMES if name in os.environ}
    text = render_launchd_plist(
        label=label,
        program_arguments=arguments,
        data_dir=resolved_data_dir,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        working_directory=resolved_data_dir,
        environment=proxy_env,
    )
    target = plist_path(launch_agents_dir, label)
    _atomic_write(target, text, 0o600 if proxy_env else 0o644)

    launchctl = runner or _run_launchctl
    domain = _domain(uid)
    job = f"{domain}/{label}"
    launchctl(["bootout", job])
    if not _wait_for_job_exit(launchctl, job):
        raise ServiceInstallError(f"等待 launchd 作业退出超时：{job}\nplist 已保留：{target}")
    bootstrap = ["bootstrap", domain, str(target)]
    result = launchctl(bootstrap)
    if result.returncode != 0:
        sleep(_BOOTSTRAP_RETRY_DELAY)
        result = launchctl(bootstrap)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "无输出").strip()
        raise ServiceInstallError(
            f"launchctl bootstrap 失败（退出码 {result.returncode}）：{detail}\n"
            f"plist 已保留：{target}\n"
            f"功能开关：{config_path}（{describe_features(features)}）"
        )
    return ServiceInstallResult(
        plist_path=target,
        config_path=config_path,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        bootstrapped=True,
        features=features,
    )


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
    runner: LaunchctlRunner | None = None,
    uid: int | None = None,
) -> ServiceStatus:
    target = plist_path(launch_agents_dir, label)
    installed = target.exists()
    loaded = False
    state: str | None = None
    pid: int | None = None
    detail: str | None = None
    proxy_endpoints: tuple[str, ...] = ()

    if installed:
        launchctl = runner or _run_launchctl
        result = launchctl(["print", f"{_domain(uid)}/{label}"])
        if result.returncode == 0:
            loaded = True
            state = _launchctl_value(result.stdout, "state")
            raw_pid = _launchctl_value(result.stdout, "pid")
            pid = int(raw_pid) if raw_pid is not None and raw_pid.isdigit() else None
            if data_dir is None:
                data_dir = _job_data_dir(
                    _launchctl_arguments(result.stdout),
                    _launchctl_value(result.stdout, "working directory"),
                )
        else:
            detail = (result.stderr or result.stdout or "").strip() or (
                f"launchctl print 退出码 {result.returncode}"
            )

    if installed:
        try:
            job_plist = _read_job_plist(target)
            proxy_endpoints = _plist_proxy_endpoints(job_plist)
            if data_dir is None:
                data_dir = _plist_data_dir(job_plist, target)
        except ValueError as exc:
            detail = _merge_detail(detail, str(exc))
    if data_dir is None:
        data_dir = os.environ.get("TALARIA_DATA_DIR", str(DEFAULT_DATA_DIR))
    resolved_data_dir = Path(data_dir).expanduser().resolve()
    holder = probe_lock(resolved_data_dir)
    endpoint: str | None = None
    features: FeatureToggles | None = None
    if holder is not None and holder.role == ROLE_SERVICE:
        try:
            descriptor = read_service_descriptor(resolved_data_dir)
        except ServiceDescriptorError as exc:
            detail = _merge_detail(detail, str(exc))
        else:
            if descriptor is not None and holder.pid == descriptor.pid:
                endpoint = descriptor.base_url
                features = descriptor.features
            elif descriptor is not None:
                detail = _merge_detail(
                    detail,
                    f"锁持有者 pid={holder.pid} 与接口描述 pid={descriptor.pid} 不一致",
                )

    config = None
    try:
        config = load_service_config(resolved_data_dir)
    except ServiceConfigError as exc:
        if features is None:
            raise
        detail = _merge_detail(detail, str(exc))

    installed_desktop = desktop_installed()
    return ServiceStatus(
        plist_path=target,
        config_path=service_config_path(resolved_data_dir),
        installed=installed,
        loaded=loaded,
        config_exists=config is None or config.exists,
        features=features,
        disk_features=config.features if config is not None else None,
        state=state,
        pid=pid,
        lock_holder=holder,
        endpoint=endpoint,
        detail=detail,
        dsh=ServiceDshStatus(
            installed_desktop, installed_desktop and desktop_running(), desktop_plugin_installed()
        ),
        proxy_endpoints=proxy_endpoints,
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


def _plist_proxy_endpoints(plist: dict[str, object]) -> tuple[str, ...]:
    environment = plist.get("EnvironmentVariables")
    if not isinstance(environment, dict):
        return ()
    endpoints = []
    for key in _PROXY_VARIABLES:
        for name in (key, key.lower()):
            value = environment.get(name)
            if isinstance(value, str) and value.strip():
                endpoints.append(f"{name}={_proxy_endpoint(value.strip())}")
    return tuple(endpoints)


def _proxy_endpoint(value: str) -> str:
    try:
        url = urlsplit(value if "://" in value or value.startswith("//") else f"//{value}")
        host, port = url.hostname, url.port
    except ValueError:
        return "地址无效"
    if not host:
        return "地址无效"
    if port is None:
        return host
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


def _merge_detail(existing: str | None, extra: str) -> str:
    return f"{existing}；{extra}" if existing else extra


def _atomic_write(path: Path, text: str, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
