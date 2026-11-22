"""读取常驻服务为本机客户端发布的回环地址。"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from talaria.service_lock import ROLE_SERVICE, probe_lock

DESCRIPTOR_NAME = "service-descriptor.json"
DESCRIPTOR_VERSION = 1
DATA_DIR_HEADER = "X-Talaria-Data-Dir"
NOW_MS_HEADER = "X-Talaria-Now-Ms"
BARS_MANIFEST_HEADER = "X-Talaria-Bars-Manifest"


class ServiceDescriptorError(ValueError):
    """服务接口描述损坏或不符合回环约束。"""


def service_descriptor_path(data_dir: str | Path) -> Path:
    return Path(data_dir).resolve() / "runtime" / DESCRIPTOR_NAME


def data_dir_id(data_dir: str | Path) -> str:
    """标识解析后的数据目录路径。"""
    return hashlib.sha256(os.fsencode(Path(data_dir).resolve())).hexdigest()


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
    return address.compressed.partition("%")[0]


def loopback_base_url(host: str, port: int) -> str:
    return f"http://{f'[{host}]' if ':' in host else host}:{port}"


def normalize_base_url(base_url: object, path: Path) -> str:
    if not isinstance(base_url, str):
        raise ServiceDescriptorError(f"服务接口描述 {path} 缺少 base_url")
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


def read_descriptor_payload(path: Path) -> dict[str, object] | None:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError) as exc:
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
    return payload


def _read_active_descriptor(data_dir: str | Path) -> tuple[str, dict[str, object]] | None:
    holder = probe_lock(data_dir)
    if holder is None or holder.role != ROLE_SERVICE or holder.pid is None:
        return None
    path = service_descriptor_path(data_dir)
    try:
        payload = read_descriptor_payload(path)
        if payload is None:
            return None
        pid = payload.get("pid")
        if type(pid) is not int or pid <= 0 or pid != holder.pid:
            return None
        return normalize_base_url(payload.get("base_url"), path), payload
    except ServiceDescriptorError:
        return None


def read_service_endpoint(data_dir: str | Path) -> str | None:
    descriptor = _read_active_descriptor(data_dir)
    return None if descriptor is None else descriptor[0]


@dataclass(frozen=True, slots=True)
class ServiceConnection:
    base_url: str
    token: str


def read_service_connection(data_dir: str | Path) -> ServiceConnection | None:
    descriptor = _read_active_descriptor(data_dir)
    if descriptor is None:
        return None
    base_url, payload = descriptor
    token = payload.get("token")
    if not isinstance(token, str) or not token:
        return None
    return ServiceConnection(base_url, token)
