"""DSH 协同宿主的子进程管理：启动、健康探针、日志分流与优雅退出。

预研边界：
- 只通过 ``dsh`` CLI 启动一个独立 Profile，不直接依赖 DSH 内部模块；
- DSH Web 使用随机 token，stdout 打印的 URL 是唯一可信的入口；
- 宿主崩溃后独立重启，保证看盘快通道继续工作。
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from concurrent.futures import Future
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from talaria.workbench_api.service_descriptor import validate_loopback_host

DEFAULT_DSH_PORT = 8766

_URL_PATTERN = re.compile(r"https?://[^\s]+")
_ANSI_PATTERN = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_SECRET_PATTERN = re.compile(r"([?&]token=)[^\s&]+")
_ENTRY_NAME = "dsh-entry.json"
_TAIL_LINES = 30
_MAX_RESTART_DELAY = 60.0
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
    candidate = explicit or os.environ.get("TALARIA_DSH_BIN") or "dsh"
    if os.path.sep in candidate:
        path = Path(candidate).expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
        raise DshHostError(
            f"DSH 可执行文件不存在或不可执行：{path}\n"
            "请安装 DeepSeek Harness 0.2.2-rc.3 或用 --dsh-bin 指定 dsh 路径；"
            "单机模式可用 talaria up --solo。"
        )
    found = shutil.which(candidate)
    if found is None:
        raise DshHostError(
            f"未找到 DSH 可执行文件：{candidate}\n"
            "请先安装 DeepSeek Harness（dsh --version 应为 0.2.2-rc.3），"
            "或用 --dsh-bin 指定路径；单机模式可用 talaria up --solo。"
        )
    return found


def bridge_descriptor_path(data_dir: Path) -> Path:
    return data_dir / "runtime" / "dsh-bridge.json"


def dsh_entry_path(data_dir: Path) -> Path:
    return data_dir / "runtime" / _ENTRY_NAME


def read_dsh_entry(data_dir: Path, *, daemon_pid: int, pid: int) -> str | None:
    try:
        entry = json.loads(dsh_entry_path(data_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if (
        not isinstance(entry, dict)
        or entry.get("daemon_pid") != daemon_pid
        or entry.get("pid") != pid
    ):
        return None
    url = entry.get("url")
    return url if isinstance(url, str) and is_local_dsh_url(url) else None


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
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def remove_bridge_descriptor(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return


class DshHost:
    """一个可被守护进程监督的 DSH Web 子进程。"""

    def __init__(
        self,
        *,
        dsh_bin: str,
        profile: str,
        host: str,
        port: int,
        bridge_path: Path,
        log_dir: Path,
        startup_timeout: float = 40.0,
        on_offline: Callable[[DshHostInfo], None] | None = None,
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
            self._dsh_bin,
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
        self._log_dir.mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        env["TALARIA_DSH_BRIDGE"] = str(self._bridge_path)
        try:
            process = self._popen_factory(
                self.command(),
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
        raise DshHostError(self._failure_message(reason, tail.copy()))

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
            if self.probe():
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
                    self._on_offline(self.info())
                self._stopping.wait(delay)
                delay = min(delay * 2, _MAX_RESTART_DELAY)
        finally:
            self._cleanup_attempt()

    def _mark_offline(self, detail: str) -> None:
        with self._lock:
            self._state = "offline"
            self._detail = redact_dsh_token(detail)

    def _failure_message(self, reason: str, tail: deque[str]) -> str:
        output = "\n".join(tail) if tail else "（无输出）"
        return (
            f"DSH 协同宿主启动失败（{reason}）。\n"
            f"命令：{' '.join(self.command())}\n"
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
        try:
            process.wait(timeout=self._stop_timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except PermissionError:
                process.kill()
            process.wait(timeout=3)
        if self._reader is not None:
            self._reader.join(timeout=2)
        with self._lock:
            self._process = None
            self._url = None
