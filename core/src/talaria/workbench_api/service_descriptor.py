"""常驻服务对同机命令发布的最小写入接口描述。"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from talaria.service_endpoint import (
    DESCRIPTOR_NAME as DESCRIPTOR_NAME,
    DESCRIPTOR_VERSION as DESCRIPTOR_VERSION,
    ServiceDescriptorError as ServiceDescriptorError,
    loopback_base_url as loopback_base_url,
    normalize_base_url,
    read_descriptor_payload,
    service_descriptor_path as service_descriptor_path,
    validate_loopback_host as validate_loopback_host,
)
from talaria.workbench_api.service_config import FEATURE_NAMES, FeatureToggles


@dataclass(frozen=True, slots=True)
class ServiceDescriptor:
    base_url: str
    token: str
    pid: int
    features: FeatureToggles


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
    base_url = normalize_base_url(base_url, destination)
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
    payload = read_descriptor_payload(path)
    if payload is None:
        return None
    base_url = normalize_base_url(payload.get("base_url"), path)
    token = payload.get("token")
    pid = payload.get("pid")
    raw_features = payload.get("features")
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
