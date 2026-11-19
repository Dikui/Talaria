"""DSH 协同宿主的子进程管理：启动、健康探针、日志分流与优雅退出。

预研边界：
- 只通过 ``dsh`` CLI 启动一个独立 Profile，不直接依赖 DSH 内部模块；
- DSH Web 使用随机 token，stdout 打印的 URL 是唯一可信的入口；
- 宿主崩溃只标记离线，不结束 Python 守护进程，保证看盘快通道继续工作。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

_URL_PATTERN = re.compile(r"https?://[^\s]+")
_ANSI_PATTERN = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_SECRET_PATTERN = re.compile(r"([?&]token=)[^\s&]+")
_TAIL_LINES = 30


class DshHostError(RuntimeError):
    """宿主缺失、启动失败或健康探针失败。"""


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


def write_bridge_descriptor(
    path: Path,
    *,
    base_url: str,
    token: str,
    daemon_pid: int,
) -> Path:
    """把回环地址与 Bearer token 以 0600 权限写入运行目录。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.parent.chmod(0o700)
    except OSError:  # pragma: no cover - 非 POSIX 平台
        pass
    payload = {
        "version": 1,
        "base_url": base_url,
        "token": token,
        "daemon_pid": daemon_pid,
        "created_at": int(time.time()),
    }
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    try:
        temporary.chmod(0o600)
    except OSError:  # pragma: no cover - 非 POSIX 平台
        pass
    temporary.replace(path)
    return path


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
        self._log_dir = log_dir
        self._startup_timeout = startup_timeout
        self._on_offline = on_offline
        self._popen_factory = popen_factory
        self._lock = threading.Lock()
        self._state = "starting"
        self._detail = "等待 DSH 启动"
        self._url: str | None = None
        self._base_url: str | None = None
        self._log_path = ""
        self._process: subprocess.Popen[str] | None = None
        self._reader: threading.Thread | None = None
        self._tail: deque[str] = deque(maxlen=_TAIL_LINES)
        self._url_ready = threading.Event()
        self._stopping = False

    # ------------------------------------------------------------------ 查询
    def info(self) -> DshHostInfo:
        with self._lock:
            return DshHostInfo(
                state=self._state,
                url=self._url,
                detail=self._detail,
                log_path=self._log_path,
                pid=self._process.pid if self._process is not None else None,
            )

    def status_payload(self) -> dict[str, object]:
        return self.info().payload()

    @property
    def log_path(self) -> Path:
        return Path(self._log_path) if self._log_path else self._log_dir

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
        self._log_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        self._log_path = str(self._log_dir / f"dsh-host-{stamp}-{os.getpid()}.log")
        env = os.environ.copy()
        env["TALARIA_DSH_BRIDGE"] = str(self._bridge_path)
        try:
            process = self._popen_factory(
                self.command(),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                env=env,
                start_new_session=True,
            )
        except OSError as exc:
            raise DshHostError(f"无法启动 DSH 宿主：{exc}") from exc
        self._process = process
        self._reader = threading.Thread(
            target=self._read_output, name="talaria-dsh-log", daemon=True
        )
        self._reader.start()
        deadline = time.monotonic() + self._startup_timeout
        while time.monotonic() < deadline:
            if process.poll() is not None:
                self._mark_offline(f"DSH 启动失败（退出码 {process.returncode}）")
                raise DshHostError(self._failure_message())
            if self._url_ready.wait(timeout=0.2):
                break
        else:
            self.stop()
            self._mark_offline("DSH 启动超时")
            raise DshHostError(self._failure_message())
        if not self._probe_until_ready(deadline):
            self.stop()
            self._mark_offline("DSH 健康探针未通过")
            raise DshHostError(self._failure_message())
        with self._lock:
            self._state = "online"
            self._detail = "健康探针通过"
        threading.Thread(
            target=self._watch_exit, name="talaria-dsh-watch", daemon=True
        ).start()
        return self.info()

    def _read_output(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            return
        log_file = open(self._log_path, "a", encoding="utf-8")
        try:
            for raw in process.stdout:
                line = _ANSI_PATTERN.sub("", raw.rstrip("\n"))
                if not line:
                    continue
                self._tail.append(line)
                log_file.write(_SECRET_PATTERN.sub(r"\1***", line) + "\n")
                log_file.flush()
                match = _URL_PATTERN.search(line)
                if match is not None and self._url is None:
                    self._url = match.group(0)
                    self._base_url = self._url.split("?", 1)[0]
                    self._url_ready.set()
        finally:
            log_file.close()

    def _probe_until_ready(self, deadline: float) -> bool:
        while time.monotonic() < deadline:
            if self._process is not None and self._process.poll() is not None:
                return False
            if self.probe():
                return True
            time.sleep(0.25)
        return False

    def probe(self) -> bool:
        if self._url is None:
            return False
        request = urllib.request.Request(self._url, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=2.0) as response:
                return response.status < 500
        except urllib.error.HTTPError as exc:
            # 401/403 说明 Web 服务已监听；健康探针不负责登录。
            return exc.code < 500
        except (urllib.error.URLError, OSError, TimeoutError):
            return False

    def _watch_exit(self) -> None:
        process = self._process
        if process is None:
            return
        code = process.wait()
        if self._stopping:
            return
        self._mark_offline(f"DSH 宿主意外退出（退出码 {code}）")
        if self._on_offline is not None:
            self._on_offline(self.info())

    def _mark_offline(self, detail: str) -> None:
        with self._lock:
            if self._state == "offline":
                return
            self._state = "offline"
            self._detail = detail

    def _failure_message(self) -> str:
        tail = "\n".join(self._tail) if self._tail else "（无输出）"
        return (
            "DSH 协同宿主启动失败。\n"
            f"命令：{' '.join(self.command())}\n"
            f"日志：{self._log_path}\n"
            f"最近输出：\n{tail}\n"
            "请先运行 talaria dsh-install 安装 Profile 与插件；"
            "单机模式可用 talaria up --solo。"
        )

    # ------------------------------------------------------------------ 停止
    def stop(self, timeout: float = 8.0) -> None:
        process = self._process
        if process is None:
            return
        self._stopping = True
        with self._lock:
            if self._state != "offline":
                self._state = "stopped"
                self._detail = "已由守护进程停止"
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                process.terminate()
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    process.kill()
                process.wait(timeout=3)
        if self._reader is not None:
            self._reader.join(timeout=2)
        self._process = None
