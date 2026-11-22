"""常驻服务对同机命令发布的最小写入接口描述。"""

from __future__ import annotations

import ipaddress
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from talaria.workbench_api.service_config import FEATURE_NAMES, FeatureToggles

DESCRIPTOR_NAME = "service-descriptor.json"
DESCRIPTOR_VERSION = 1


class ServiceDescriptorError(ValueError):
    """服务接口描述损坏或不符合回环约束。"""


@dataclass(frozen=True, slots=True)
class ServiceDescriptor:
    base_url: str
    token: str
    pid: int
    features: FeatureToggles


def service_descriptor_path(data_dir: str | Path) -> Path:
    return Path(data_dir).resolve() / "runtime" / DESCRIPTOR_NAME


def validate_loopback_host(host: str, *, allow_localhost: bool = False) -> str:
    """校验并规范化回环 IP，描述文件兼容旧的 localhost 地址。"""
    if allow_localhost and host == "localhost":
        return host
    try:
        address = ipaddress.ip_address(host)
    except ValueError as exc:
        raise ValueError(f"host 必须是回环 IP 地址：{host}") from exc
    if not address.is_loopback:
        raise ValueError(f"只允许绑定回环地址，拒绝：{host}")
    # 回环地址不需要 IPv6 作用域，去掉后可直接用于绑定和 URL。
    return address.compressed.partition("%")[0]


def loopback_base_url(host: str, port: int) -> str:
    return f"http://{f'[{host}]' if ':' in host else host}:{port}"


def write_service_descriptor(
    path: str | Path,
    *,
    base_url: str,
    token: str,
    pid: int,
    features: FeatureToggles,
) -> Path:
    """原子写入 0600 描述文件，仅本机命令可读。"""

    destination = Path(path)
    base_url = _normalize_base_url(base_url, destination)
    if not token:
        raise ServiceDescriptorError(f"服务接口描述 {destination} 缺少 token")
    if pid <= 0:
        raise ServiceDescriptorError(f"服务接口描述 {destination} 的 pid 非法：{pid}")

    payload = {
        "version": DESCRIPTOR_VERSION,
        "base_url": base_url,
        "token": token,
        "pid": pid,
        "features": {name: getattr(features, name) for name in FEATURE_NAMES},
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    _atomic_write(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n", 0o600)
    return destination


def read_service_descriptor(data_dir: str | Path) -> ServiceDescriptor | None:
    path = service_descriptor_path(data_dir)
    if not path.exists():
        return None
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ServiceDescriptorError(f"无法读取服务接口描述 {path}：{exc}") from exc
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ServiceDescriptorError(f"服务接口描述 {path} 不是合法 JSON") from exc
    if not isinstance(payload, dict):
        raise ServiceDescriptorError(f"服务接口描述 {path} 的顶层必须是 JSON 对象")
    if payload.get("version") != DESCRIPTOR_VERSION:
        raise ServiceDescriptorError(
            f"服务接口描述 {path} 的 version 不受支持：{payload.get('version')!r}"
        )

    base_url = payload.get("base_url")
    token = payload.get("token")
    pid = payload.get("pid")
    raw_features = payload.get("features")
    if not isinstance(base_url, str):
        raise ServiceDescriptorError(f"服务接口描述 {path} 缺少 base_url")
    base_url = _normalize_base_url(base_url, path)
    if not isinstance(token, str) or not token:
        raise ServiceDescriptorError(f"服务接口描述 {path} 缺少 token")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        raise ServiceDescriptorError(f"服务接口描述 {path} 的 pid 非法")
    if not isinstance(raw_features, dict):
        raise ServiceDescriptorError(f"服务接口描述 {path} 缺少 features 对象")

    values: dict[str, bool] = {}
    for name in FEATURE_NAMES:
        value = raw_features.get(name, True)
        if not isinstance(value, bool):
            raise ServiceDescriptorError(
                f"服务接口描述 {path} 的功能开关 {name} 必须是 true 或 false"
            )
        values[name] = value
    return ServiceDescriptor(
        base_url=base_url,
        token=token,
        pid=pid,
        features=FeatureToggles(**values),
    )


def remove_service_descriptor(path: str | Path) -> None:
    Path(path).unlink(missing_ok=True)


def _normalize_base_url(base_url: str, path: Path) -> str:
    try:
        parsed = urlsplit(base_url)
        port = parsed.port
        host = validate_loopback_host(parsed.hostname or "", allow_localhost=True)
        if (
            parsed.scheme == "http"
            and port is not None and port > 0
            and parsed.username is None
            and parsed.password is None
            and parsed.path in ("", "/")
            and not parsed.query
            and not parsed.fragment
        ):
            return loopback_base_url(host, port)
    except ValueError:
        pass
    raise ServiceDescriptorError(
        f"服务接口描述 {path} 的 base_url 必须是无路径的本机 http 地址，收到 {base_url!r}"
    )


def _atomic_write(path: Path, text: str, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
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
