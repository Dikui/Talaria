"""DSH 协同宿主的子进程管理：启动、健康探针、日志分流与优雅退出。

预研边界：
- 只通过 ``dsh`` CLI 启动一个独立 Profile，不直接依赖 DSH 内部模块；
- DSH Web 使用随机 token，stdout 打印的 URL 是唯一可信的入口；
- 宿主崩溃后独立重启，保证看盘快通道继续工作。
"""

from __future__ import annotations

import fcntl
import gzip
import hashlib
import io
import json
import logging
import os
import re
import shutil
import signal
import stat
import subprocess
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from concurrent.futures import Future
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import urlsplit

import yaml

from talaria.service_endpoint import validate_loopback_host

DEFAULT_DSH_PORT = 8766
DESKTOP_PROFILE = "desktop"
DESKTOP_APP = Path("/Applications/DeepSeek Harness.app")
DESKTOP_APP_NAME = "DeepSeek Harness"
DESKTOP_DSH_BIN = DESKTOP_APP / "Contents/Resources/runtime/cli/bin/dsh"
PLUGIN_NAME = "@talaria/dsh-plugin"

_PROFILE_PATCH = "cordis.patch.yml"
_PATCH_START = "# >>> talaria (added by Talaria; remove this block to uninstall)"
_PATCH_END = "# <<< talaria"
_PATCH_MARKER = re.compile(rf"(?m)^({re.escape(_PATCH_START)}|{_PATCH_END})[ \t]*(?:\r?\n|\Z)")
_EMPTY_PATCH = re.compile(r"(?m)^[ \t]*\[[ \t]*\][ \t]*(?:#[^\r\n]*)?(?:\r?\n|\Z)")
_EMPTY_PATCH_COMMENT = "# Talaria saved the empty patch: "
_ADDED_NEWLINE_COMMENT = "# Talaria added the preceding line break"
_URL_PATTERN = re.compile(r"https?://[^\s]+")
_ANSI_PATTERN = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_SECRET_PATTERN = re.compile(r"([?&]token=)[^\s&]+")
_ENTRY_NAME = "dsh-entry.json"
_TAIL_LINES = 30
_MAX_RESTART_DELAY = 60.0
_ORPHAN_STOP_TIMEOUT = 0.5
_LOGGER = logging.getLogger(__name__)


class DshHostError(RuntimeError):
    """宿主缺失、启动失败或健康探针失败。"""


def redact_dsh_token(text: str) -> str:
    return _SECRET_PATTERN.sub(r"\1***", text)


def is_local_dsh_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        validate_loopback_host(parsed.hostname or "", allow_localhost=True)
        return (
            parsed.scheme in {"http", "https"}
            and parsed.port != 0
            and parsed.username is None
            and parsed.password is None
            and not any(character.isspace() for character in url)
        )
    except ValueError:
        return False


@dataclass(frozen=True)
class DshHostInfo:
    """供 ``/api/collaboration`` 与 CLI 输出使用的只读状态。"""

    state: str
    url: str | None
    detail: str
    log_path: str
    pid: int | None

    @property
    def base_url(self) -> str | None:
        """仅供桥接派发的无查询串 URL；入口 token 不进入 API。"""
        return self.url.split("?", 1)[0] if self.url else None

    def payload(self) -> dict[str, object]:
        # 宿主入口的 token 只用于人工打开 DSH；API 不把它暴露给浏览器。
        base_url = self.base_url
        return {
            "mode": "dual",
            "host": self.state,
            "url": base_url,
            "detail": self.detail,
            "log_path": self.log_path,
            "pid": self.pid,
            "message": _state_message(self),
        }


def _state_message(info: DshHostInfo) -> str:
    if info.state == "online":
        return "DSH 协同宿主在线；图表快通道独立于宿主。"
    if info.state == "starting":
        return "DSH 协同宿主启动中；图表快通道已可用。"
    if info.state == "offline":
        return "DSH 协同宿主离线；图表快通道仍可用，切图与行情不受影响。"
    return "DSH 协同宿主已停止；图表快通道仍可用。"


def resolve_dsh_bin(explicit: str | None) -> str:
    """解析 DSH 可执行文件，缺失时给出可执行的中文安装提示。"""
    candidate = explicit or os.environ.get("TALARIA_DSH_BIN")
    if not candidate:
        candidate = shutil.which("dsh") or str(DESKTOP_DSH_BIN)
    if os.path.sep in candidate:
        path = Path(candidate).expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            return str(path.absolute())
        raise DshHostError(
            f"DSH 可执行文件不存在或不可执行：{path}\n"
            "请安装 DeepSeek Harness 0.2.2-rc.5 或用 --dsh-bin 指定 dsh 路径；"
            "单机模式可用 talaria up --solo。"
        )
    found = shutil.which(candidate)
    if found is None:
        raise DshHostError(
            f"未找到 DSH 可执行文件：{candidate}\n"
            "请先安装 DeepSeek Harness（dsh --version 应为 0.2.2-rc.5），"
            "或用 --dsh-bin 指定路径；单机模式可用 talaria up --solo。"
        )
    return str(Path(found).absolute())


def dsh_profile_dir(profile: str) -> Path:
    if profile in {"", ".", "..", "node_modules"} or "/" in profile or "\\" in profile:
        raise ValueError(f"无效的 DSH Profile 名称：{profile!r}")
    home = Path(os.environ.get("DSH_HOME") or "~/.dsh").expanduser().absolute()
    return home / "profiles" / profile


def desktop_installed() -> bool:
    return DESKTOP_APP.is_dir()


def desktop_running() -> bool:
    executable = DESKTOP_APP / "Contents/MacOS" / DESKTOP_APP_NAME
    # The bundled CLI also runs this executable, with --expose-internals and a JS entry.
    try:
        result = subprocess.run(
            ["/usr/bin/pgrep", "-f", f"^{re.escape(str(executable))}( -psn_[0-9_]+)?$"],
            capture_output=True,
            check=False,
        )
    except OSError:
        return False
    return result.returncode == 0


def desktop_plugin_installed() -> bool:
    profile = dsh_profile_dir(DESKTOP_PROFILE)
    try:
        if profile_has_talaria_patch(profile):
            return True
    except (OSError, ValueError):
        pass
    if not (profile / "node_modules" / PLUGIN_NAME / "package.json").is_file():
        return False
    try:
        manifest = json.loads((profile / "package.json").read_text(encoding="utf-8"))
        bundles = manifest["dsh"]["profile"]["bundles"]
        return isinstance(bundles, list) and PLUGIN_NAME in bundles
    except (OSError, ValueError, KeyError, TypeError):
        return False


def read_desktop_service_config() -> dict[str, object] | None:
    path = dsh_profile_dir(DESKTOP_PROFILE) / _PROFILE_PATCH
    try:
        original = path.read_text(encoding="utf-8")
        span = _talaria_patch_span(original)
        if span is None:
            return None
        # 只解析 Talaria 管理的段，用户的其他补丁可使用 DSH 专有语法。
        start, end = span
        operations = yaml.safe_load(original[start:end])
    except (OSError, ValueError, yaml.YAMLError):
        return None
    if not isinstance(operations, list):
        return None
    for operation in operations:
        plugins = operation.get("insert") if isinstance(operation, dict) else None
        if not isinstance(plugins, list):
            continue
        for plugin in plugins:
            if isinstance(plugin, dict) and plugin.get("id") == "talaria-desk-plugin":
                config = plugin.get("config")
                service = config.get("service") if isinstance(config, dict) else None
                return service if isinstance(service, dict) else None
    return None


def _plugin_patch(
    source: Path, data_dir: Path, *,
    entry: Path | None = None,
    service: dict[str, object] | None = None,
) -> str:
    patch = (source / _PROFILE_PATCH).read_text(encoding="utf-8").rstrip()
    credential = json.dumps(str(bridge_descriptor_path(data_dir)), ensure_ascii=False)
    tools_block = f"          - id: talaria-desk-tools\n            name: '{PLUGIN_NAME}/tools'"
    if patch.count(tools_block) != 1:
        raise ValueError(f"无法在 {source / _PROFILE_PATCH} 中定位 Talaria 工具插件项")
    tools_specifier = (
        json.dumps(str(entry.with_name("tools.js")), ensure_ascii=False)
        if entry is not None
        else f"'{PLUGIN_NAME}/tools'"
    )
    patch = patch.replace(
        tools_block,
        "          - id: talaria-desk-tools\n"
        f"            name: {tools_specifier}\n"
        "            config:\n"
        f"              credentialFile: {credential}",
        1,
    )
    if entry is not None:
        package_line = f"      name: '{PLUGIN_NAME}'"
        if not patch.endswith(f"    - id: talaria-desk-plugin\n{package_line}"):
            raise ValueError(f"无法在 {source / _PROFILE_PATCH} 中定位 Talaria 插件项")
        specifier = json.dumps(str(entry), ensure_ascii=False)
        patch = patch.removesuffix(package_line) + f"      name: {specifier}"
    patch += f"\n      config:\n        credentialFile: {credential}\n"
    if entry is not None:
        patch += "        desktop: true\n"
    if service is not None:
        patch += "        service:\n"
        for name, value in service.items():
            patch += f"          {name}: {json.dumps(value, ensure_ascii=False)}\n"
    return patch


def _talaria_patch_span(text: str) -> tuple[int, int] | None:
    markers = list(_PATCH_MARKER.finditer(text))
    if not markers:
        return None
    if [marker[1] for marker in markers] != [_PATCH_START, _PATCH_END]:
        raise ValueError(f"{_PROFILE_PATCH} 中的 Talaria 标记不完整或重复")
    return markers[0].start(), markers[1].end()


def profile_has_talaria_patch(profile_dir: Path) -> bool:
    try:
        original = (profile_dir / _PROFILE_PATCH).read_bytes().decode("utf-8")
    except FileNotFoundError:
        return False
    return _talaria_patch_span(original) is not None


def _only_comments(text: str) -> bool:
    return all(not line.strip() or line.lstrip().startswith("#") for line in text.splitlines())


def _edit_talaria_patch(original: str, patch: str | None) -> str:
    span = _talaria_patch_span(original)
    newline = "\r\n" if "\r\n" in original else "\n"
    saved_empty = ""
    added_newline = False
    has_final_newline = True
    if span is not None:
        start, end = span
        block = original[start:end]
        marker, _, body = block.partition("\n")
        newline = "\r\n" if marker.endswith("\r") else "\n"
        annotation = body.partition("\n")[0].removesuffix("\r")
        saved = annotation.removeprefix(_EMPTY_PATCH_COMMENT)
        if saved != annotation and _EMPTY_PATCH.fullmatch(saved):
            saved_empty = saved
        added_newline = annotation == _ADDED_NEWLINE_COMMENT
        has_final_newline = block.endswith("\n")
        if patch is None:
            if added_newline and end == len(original) and original[:start].endswith(newline):
                start -= len(newline)
            before, after = original[:start], original[end:]
            remaining = before + after
            if _only_comments(remaining):
                if saved_empty:
                    restored = saved_empty + (newline if has_final_newline else "")
                    return before + restored + after
                # DSH requires an array even when the Talaria block was the only entry.
                separator = newline if remaining and not remaining.endswith("\n") else ""
                return remaining + separator + "[]" + newline
            if saved_empty and "#" in saved_empty:
                return before + "#" + saved_empty.partition("#")[2] + newline + after
            return remaining
    elif patch is None:
        return original
    else:
        empty = _EMPTY_PATCH.search(original)
        if empty and _only_comments(original[:empty.start()] + original[empty.end():]):
            # DSH initializes the patch to an empty flow sequence.
            start, end = empty.span()
            saved_empty = empty[0].rstrip("\r\n")
            has_final_newline = empty[0].endswith("\n")
            if has_final_newline:
                newline = "\r\n" if empty[0].endswith("\r\n") else "\n"
        else:
            added_newline = bool(original) and not original.endswith("\n")
            if added_newline:
                original += newline
            start = end = len(original)

    block = _PATCH_START + newline
    if saved_empty:
        block += _EMPTY_PATCH_COMMENT + saved_empty + newline
    elif added_newline:
        block += _ADDED_NEWLINE_COMMENT + newline
    block += patch.replace("\n", newline) + _PATCH_END
    if has_final_newline:
        block += newline
    return original[:start] + block + original[end:]


def _update_talaria_patch(profile_dir: Path, patch: str | None, *, private: bool = False) -> bool:
    path = profile_dir / _PROFILE_PATCH
    try:
        directory_fd = os.open(profile_dir, os.O_RDONLY)
    except FileNotFoundError:
        if patch is None:
            return False
        raise
    try:
        # Lock the directory so concurrent Talaria commands share a stable inode.
        fcntl.flock(directory_fd, fcntl.LOCK_EX)
        try:
            with path.open("rb") as handle:
                original = handle.read()
                mode = stat.S_IMODE(os.fstat(handle.fileno()).st_mode)
        except FileNotFoundError:
            if patch is None:
                return False
            original, mode = b"[]\n", None
        updated = _edit_talaria_patch(original.decode("utf-8"), patch).encode("utf-8")
        write_mode = 0o600 if private or mode is None else mode
        if updated == original and write_mode == mode:
            return False
        if mode is not None and updated != original:
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
            backup_dir = profile_dir.parent.parent / "profiles-backup" / f"{profile_dir.name}-{stamp}"
            backup_dir.mkdir(mode=0o700, parents=True)
            _write_atomic_file(backup_dir / path.name, original, mode=mode)
        _write_atomic_file(path, updated, mode=write_mode)
        return True
    finally:
        os.close(directory_fd)


def install_talaria_patch(
    profile_dir: Path, plugin_dir: Path, data_dir: Path, *,
    service: dict[str, object] | None = None,
) -> None:
    patch = _plugin_patch(
        plugin_dir, data_dir, entry=(plugin_dir / "index.js").absolute(), service=service
    )
    # 代理 URL 可能携带凭据，与旧作业的代理配置使用相同的权限。
    _update_talaria_patch(profile_dir, patch, private=bool(service and service.get("env")))


def remove_talaria_patch(profile_dir: Path) -> bool:
    return _update_talaria_patch(profile_dir, None)


def build_install_bundle(
    *, repo: Path, profile: str, data_dir: Path, tool_names: Iterable[str]
) -> Path:
    """为目标 Profile 打包 DSH 配置及已构建的插件。"""
    source = repo / "dsh/packages/talaria-dsh-plugin"
    manifest = json.loads((source / "package.json").read_text(encoding="utf-8"))
    patch = _plugin_patch(source, data_dir)
    if profile == "talaria-desk":
        restrictions = repo / "dsh/profiles/talaria-desk.patch.yml"
        patch = restrictions.read_text(encoding="utf-8") + "\n" + patch

    # A content-addressed file: tarball lets pnpm reuse an unchanged build and
    # pick up updates to code or config even when the package version is the same.
    manifest.pop("devDependencies", None)
    manifest.pop("scripts", None)
    runtime_files = ("index.js", "tools.js", "bridge.js", "guard.js", "client/index.js")
    tool_schema_dir = repo / "contracts/schemas/dsh-tools"
    entries = {
        "package.json": json.dumps(manifest).encode(),
        _PROFILE_PATCH: patch.encode(),
        **{name: (source / name).read_bytes() for name in runtime_files},
        **{f"{name}.tool.json": (tool_schema_dir / f"{name}.tool.json").read_bytes()
           for name in tool_names},
    }
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for name, payload in entries.items():
            info = tarfile.TarInfo(f"package/{name}")
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
    bundle = gzip.compress(buffer.getvalue(), mtime=0)
    digest = hashlib.sha256(bundle).hexdigest()[:16]
    directory = dsh_profile_dir(profile) / "talaria"
    directory.mkdir(mode=0o700, exist_ok=True)
    path = directory / f"talaria-dsh-plugin-{digest}.tgz"
    if not path.is_file():
        _write_atomic_file(path, bundle)
    return path


def bridge_descriptor_path(data_dir: Path) -> Path:
    return data_dir / "runtime" / "dsh-bridge.json"


def dsh_entry_path(data_dir: Path) -> Path:
    return data_dir / "runtime" / _ENTRY_NAME


def read_desktop_origin(data_dir: Path) -> str | None:
    path = data_dir / "runtime" / "dsh-desktop.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        origin, pid = payload["base_url"], payload["pid"]
        if type(pid) is not int or pid <= 0 or not isinstance(origin, str):
            return None
        parsed = urlsplit(origin)
        if not is_local_dsh_url(origin) or (
            parsed.scheme != "http" or parsed.port is None
            or origin != f"{parsed.scheme}://{parsed.netloc}"
        ):
            return None
        os.kill(pid, 0)
        return origin
    except (OSError, ValueError, OverflowError, TypeError, KeyError):
        return None


def write_bridge_descriptor(
    path: Path,
    *,
    base_url: str,
    token: str,
    daemon_pid: int,
) -> Path:
    """把回环地址与 Bearer token 以 0600 权限写入运行目录。"""
    payload = {
        "version": 1,
        "base_url": base_url,
        "token": token,
        "daemon_pid": daemon_pid,
        "created_at": int(time.time()),
    }
    _write_private_json(path, payload)
    return path


def _write_private_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    path.parent.chmod(0o700)
    _write_atomic_file(path, (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode())


def _write_atomic_file(path: Path, payload: bytes, *, mode: int = 0o600) -> None:
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fchmod(handle.fileno(), mode)
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def remove_bridge_descriptor(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return


def _wait_for_process_group_exit(pgid: int, deadline: float) -> bool:
    while True:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(0.05, remaining))


def reap_stale_dsh_groups(data_dir: Path) -> None:
    """持有服务锁时，清理描述文件记录的旧服务所启动的 DSH 进程组。"""
    handled: set[int] = set()
    for path in (dsh_entry_path(data_dir), bridge_descriptor_path(data_dir)):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        daemon_pid, pgid = payload.get("daemon_pid"), payload.get("pgid")
        if type(daemon_pid) is not int or daemon_pid <= 0:
            continue
        if type(pgid) is not int or pgid <= 1 or pgid in handled or pgid == os.getpgrp():
            continue
        try:
            os.kill(daemon_pid, 0)
        except ProcessLookupError:
            pass
        except (PermissionError, OverflowError):
            continue
        else:
            continue
        handled.add(pgid)
        try:
            os.killpg(pgid, signal.SIGTERM)
            if not _wait_for_process_group_exit(pgid, time.monotonic() + _ORPHAN_STOP_TIMEOUT):
                os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, OverflowError):
            pass
        except OSError as exc:
            _LOGGER.warning("无法清理旧的 DSH 进程组 %s：%s", pgid, redact_dsh_token(str(exc)))


class DshHost:
    """一个可被守护进程监督的 DSH Web 子进程。"""

    def __init__(
        self,
        *,
        dsh_bin: str | None,
        profile: str,
        host: str,
        port: int,
        bridge_path: Path,
        log_dir: Path,
        startup_timeout: float = 40.0,
        on_offline: Callable[[DshHostInfo], None] | None = None,
        prepare_bridge: Callable[[], None] | None = None,
        popen_factory: Callable[..., subprocess.Popen[str]] = subprocess.Popen,
    ) -> None:
        self._dsh_bin = dsh_bin
        self._profile = profile
        self._host = host
        self._port = port
        self._bridge_path = bridge_path
        self._entry_path = bridge_path.with_name(_ENTRY_NAME)
        self._log_dir = log_dir
        self._startup_timeout = startup_timeout
        self._on_offline = on_offline
        self._prepare_bridge = prepare_bridge
        self._popen_factory = popen_factory
        self._lock = threading.Lock()
        self._state = "starting"
        self._detail = "等待 DSH 启动"
        self._url: str | None = None
        stamp = time.strftime("%Y%m%d-%H%M%S")
        self._log_path = str(log_dir / f"dsh-host-{stamp}-{os.getpid()}.log")
        self._process: subprocess.Popen[str] | None = None
        self._reader: threading.Thread | None = None
        self._supervisor: threading.Thread | None = None
        self._stopping = threading.Event()
        self._stop_timeout = 8.0

    # ------------------------------------------------------------------ 查询
    def info(self) -> DshHostInfo:
        with self._lock:
            return DshHostInfo(
                state=self._state,
                url=self._url if self._state == "online" else None,
                detail=self._detail,
                log_path=self._log_path,
                pid=(
                    self._process.pid
                    if self._process is not None and self._state in {"starting", "online"}
                    else None
                ),
            )

    def status_payload(self) -> dict[str, object]:
        return self.info().payload()

    @property
    def log_path(self) -> Path:
        return Path(self._log_path)

    # ------------------------------------------------------------------ 启动
    def command(self) -> list[str]:
        return [
            resolve_dsh_bin(self._dsh_bin),
            "--profile",
            self._profile,
            "--no-open",
            "--port",
            str(self._port),
        ]

    def mark_unavailable(self, detail: str) -> None:
        """宿主没有启动时仍保留 offline 状态，供 API/CLI 查询。"""
        self._mark_offline(detail)

    def start(self) -> DshHostInfo:
        """等待首次启动结果，成功后持续监督子进程。"""
        try:
            self._start_attempt()
        except (DshHostError, OSError) as exc:
            self._cleanup_attempt()
            self._mark_offline(str(exc))
            raise DshHostError(self.info().detail) from exc
        info = self.info()
        self._launch_supervisor()
        return info

    def supervise(self) -> None:
        """在后台启动 DSH，首次启动失败也按退避重试。"""
        self._launch_supervisor()

    def _launch_supervisor(self) -> None:
        self._supervisor = threading.Thread(
            target=self._supervise, name="talaria-dsh-watch", daemon=True
        )
        self._supervisor.start()

    def _start_attempt(self) -> None:
        if self._prepare_bridge is not None:
            self._prepare_bridge()
        try:
            command = self.command()
        except RuntimeError as exc:
            raise DshHostError(str(exc)) from exc
        self._log_dir.mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        env["TALARIA_DSH_BRIDGE"] = str(self._bridge_path)
        try:
            process = self._popen_factory(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                errors="replace",
                bufsize=1,
                env=env,
                start_new_session=True,
            )
        except OSError as exc:
            raise DshHostError(f"无法启动 DSH 宿主：{exc}") from exc
        self._process = process
        # start_new_session=True 使子进程的 PID 成为 PGID，探针通过前也需要记录。
        self._update_bridge_pgid(process.pid)
        next_url: Future[str] = Future()
        tail: deque[str] = deque(maxlen=_TAIL_LINES)
        self._reader = threading.Thread(
            target=self._read_output,
            args=(process, next_url, tail),
            name="talaria-dsh-log",
            daemon=True,
        )
        self._reader.start()
        deadline = time.monotonic() + self._startup_timeout
        while not self._stopping.is_set() and time.monotonic() < deadline:
            if process.poll() is not None:
                break
            try:
                self._url = next_url.result(timeout=0.1)
                break
            except TimeoutError:
                pass
        if self._url is not None and self._probe_until_ready(process, deadline):
            with self._lock:
                _write_private_json(
                    self._entry_path,
                    {
                        "url": self._url,
                        "daemon_pid": os.getpid(),
                        "pid": process.pid,
                        "pgid": process.pid,
                    },
                )
                self._state = "online"
                self._detail = "健康探针通过"
            return
        if process.poll() is not None:
            self._reader.join(timeout=0.2)
            reason = f"退出码 {process.returncode}"
        else:
            reason = "健康探针未通过" if self._url else "等待入口超时"
        raise DshHostError(self._failure_message(reason, tail.copy(), command))

    def _update_bridge_pgid(self, pgid: int | None) -> None:
        try:
            payload = json.loads(self._bridge_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return
        if not isinstance(payload, dict) or payload.get("daemon_pid") != os.getpid():
            return
        if pgid is None:
            if "pgid" not in payload:
                return
            del payload["pgid"]
        else:
            payload["pgid"] = pgid
        _write_private_json(self._bridge_path, payload)

    def _read_output(
        self, process: subprocess.Popen[str], next_url: Future[str], tail: deque[str]
    ) -> None:
        if process.stdout is None:
            return
        try:
            with process.stdout, self.log_path.open("a", encoding="utf-8", buffering=1) as log:
                for raw in process.stdout:
                    line = _ANSI_PATTERN.sub("", raw.rstrip("\n"))
                    if not line:
                        continue
                    redacted = redact_dsh_token(line)
                    tail.append(redacted)
                    log.write(redacted + "\n")
                    if not next_url.done():
                        match = _URL_PATTERN.search(line)
                        if match is not None and is_local_dsh_url(match.group(0)):
                            next_url.set_result(match.group(0))
        except OSError:
            if not next_url.done():
                next_url.set_exception(DshHostError("无法读取 DSH 输出或写入日志"))

    def _probe_until_ready(self, process: subprocess.Popen[str], deadline: float) -> bool:
        while not self._stopping.is_set() and time.monotonic() < deadline:
            if process.poll() is not None:
                return False
            try:
                ready = self.probe()
            except Exception as exc:
                detail = redact_dsh_token(str(exc))
                raise DshHostError(f"DSH 健康探针失败（{type(exc).__name__}）：{detail}") from None
            if ready:
                return process.poll() is None and not self._stopping.is_set()
            self._stopping.wait(0.25)
        return False

    def probe(self) -> bool:
        url = self._url
        if url is None:
            return False
        request = urllib.request.Request(url, method="GET")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(request, timeout=2.0) as response:
                return response.status < 500
        except urllib.error.HTTPError as exc:
            # 401/403 说明 Web 服务已监听；健康探针不负责登录。
            return exc.code < 500
        except (urllib.error.URLError, OSError, TimeoutError):
            return False

    def _supervise(self) -> None:
        delay = 1.0
        try:
            while not self._stopping.is_set():
                try:
                    if self._process is None:
                        self._start_attempt()
                    online_since = time.monotonic()
                    process = self._process
                    assert process is not None
                    while process.poll() is None and not self._stopping.wait(0.1):
                        pass
                    if time.monotonic() - online_since >= _MAX_RESTART_DELAY:
                        delay = 1.0
                    detail = f"DSH 宿主意外退出（退出码 {process.returncode}）"
                except (DshHostError, OSError) as exc:
                    detail = str(exc)
                self._cleanup_attempt()
                if self._stopping.is_set():
                    break
                self._mark_offline(detail)
                if self._on_offline is not None:
                    try:
                        self._on_offline(self.info())
                    except OSError:
                        pass
                self._stopping.wait(delay)
                delay = min(delay * 2, _MAX_RESTART_DELAY)
        finally:
            self._cleanup_attempt()

    def _mark_offline(self, detail: str) -> None:
        with self._lock:
            self._state = "offline"
            self._detail = redact_dsh_token(detail)

    def _failure_message(self, reason: str, tail: deque[str], command: list[str]) -> str:
        output = "\n".join(tail) if tail else "（无输出）"
        return (
            f"DSH 协同宿主启动失败（{reason}）。\n"
            f"命令：{' '.join(command)}\n"
            f"日志：{self._log_path}\n"
            f"最近输出：\n{output}\n"
            "请先运行 talaria dsh-install 安装 Profile 与插件；"
            "单机模式可用 talaria up --solo。"
        )

    # ------------------------------------------------------------------ 停止
    def stop(self, timeout: float = 8.0) -> None:
        self._stop_timeout = timeout
        self._stopping.set()
        if self._supervisor is not None:
            self._supervisor.join()
        self._cleanup_attempt()
        with self._lock:
            if self._state != "offline":
                self._state = "stopped"
                self._detail = "已由守护进程停止"

    def _cleanup_attempt(self) -> None:
        try:
            self._entry_path.unlink(missing_ok=True)
        except OSError as exc:
            _LOGGER.warning("无法移除 DSH 入口：%s", redact_dsh_token(str(exc)))
        process = self._process
        if process is None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except PermissionError:
            if process.poll() is None:
                process.terminate()
        deadline = time.monotonic() + self._stop_timeout
        try:
            process.wait(timeout=self._stop_timeout)
        except subprocess.TimeoutExpired:
            pass
        try:
            if not _wait_for_process_group_exit(process.pid, deadline):
                os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except PermissionError:
            if process.poll() is None:
                process.kill()
        process.wait(timeout=3)
        if self._reader is not None:
            self._reader.join(timeout=2)
        try:
            self._update_bridge_pgid(None)
        except OSError as exc:
            _LOGGER.warning("无法清除 DSH 进程组记录：%s", redact_dsh_token(str(exc)))
        with self._lock:
            self._process = None
            self._url = None
