"""launchd 用户级 Agent 的生成、安装与状态查询。"""

from __future__ import annotations

import errno
import os
import subprocess
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape

from talaria.workbench_api.service_config import (
    FeatureToggles,
    describe_features,
    load_service_config,
    write_default_service_config,
)
from talaria.workbench_api.service_descriptor import (
    ServiceDescriptorError,
    read_service_descriptor,
)
from talaria.workbench_api.service_lock import LockHolder, probe_lock

DEFAULT_SERVICE_LABEL = "com.talaria.workbench"
SERVICE_LOG_SUBDIR = Path("logs")
SERVICE_STDOUT_NAME = "service.out.log"
SERVICE_STDERR_NAME = "service.err.log"

LaunchctlRunner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


class ServiceInstallError(RuntimeError):
    """plist 已写入但 ``launchctl bootstrap`` 失败。"""


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
class ServiceStatus:
    plist_path: Path
    config_path: Path
    installed: bool
    loaded: bool
    config_exists: bool
    features: FeatureToggles
    state: str | None
    pid: int | None
    lock_holder: LockHolder | None
    endpoint: str | None
    detail: str | None


def plist_path(launch_agents_dir: str | Path, label: str = DEFAULT_SERVICE_LABEL) -> Path:
    return Path(launch_agents_dir).expanduser() / f"{label}.plist"


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
</dict>
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
    text = render_launchd_plist(
        label=label,
        program_arguments=arguments,
        data_dir=resolved_data_dir,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        working_directory=resolved_data_dir,
    )
    target = plist_path(launch_agents_dir, label)
    _atomic_write(target, text, 0o644)

    launchctl = runner or _run_launchctl
    domain = _domain(uid)
    launchctl(["bootout", f"{domain}/{label}"])
    result = launchctl(["bootstrap", domain, str(target)])
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
    result = launchctl(["bootout", f"{_domain(uid)}/{label}"])
    if result.returncode not in (0, errno.ESRCH):
        detail = (result.stderr or result.stdout).strip() or "无输出"
        raise ServiceUninstallError(
            f"launchctl bootout 失败（退出码 {result.returncode}）：{detail}\n"
            f"plist 已保留：{target}"
        )
    target.unlink()
    return ServiceUninstallResult(plist_path=target, removed=True)


def service_status(
    *,
    data_dir: str | Path,
    launch_agents_dir: str | Path,
    label: str = DEFAULT_SERVICE_LABEL,
    runner: LaunchctlRunner | None = None,
    uid: int | None = None,
) -> ServiceStatus:
    resolved_data_dir = Path(data_dir).expanduser().resolve()
    config = load_service_config(resolved_data_dir)
    target = plist_path(launch_agents_dir, label)
    installed = target.exists()
    loaded = False
    state: str | None = None
    pid: int | None = None
    detail: str | None = None

    if installed:
        launchctl = runner or _run_launchctl
        result = launchctl(["print", f"{_domain(uid)}/{label}"])
        if result.returncode == 0:
            loaded = True
            state = _launchctl_value(result.stdout, "state")
            raw_pid = _launchctl_value(result.stdout, "pid")
            pid = int(raw_pid) if raw_pid is not None and raw_pid.isdigit() else None
        else:
            detail = (result.stderr or result.stdout or "").strip() or (
                f"launchctl print 退出码 {result.returncode}"
            )

    holder = probe_lock(resolved_data_dir)
    endpoint: str | None = None
    if holder is not None:
        try:
            descriptor = read_service_descriptor(resolved_data_dir)
        except ServiceDescriptorError as exc:
            detail = _merge_detail(detail, str(exc))
        else:
            if descriptor is not None and holder.pid == descriptor.pid:
                endpoint = descriptor.base_url
            elif descriptor is not None:
                detail = _merge_detail(
                    detail,
                    f"锁持有者 pid={holder.pid} 与接口描述 pid={descriptor.pid} 不一致",
                )

    return ServiceStatus(
        plist_path=target,
        config_path=config.path,
        installed=installed,
        loaded=loaded,
        config_exists=config.exists,
        features=config.features,
        state=state,
        pid=pid,
        lock_holder=holder,
        endpoint=endpoint,
        detail=detail,
    )


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
