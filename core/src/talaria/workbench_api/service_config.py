"""数据目录中的常驻服务配置：功能开关是唯一可调项。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

SERVICE_CONFIG_NAME = "service.json"
CONFIG_VERSION = 1

FEATURE_NAMES: tuple[str, ...] = ("market_data", "cold_store", "alerts", "scanner")
FEATURE_LABELS: dict[str, str] = {
    "market_data": "行情采集",
    "cold_store": "冷库写入",
    "alerts": "预警",
    "scanner": "扫描",
}


class ServiceConfigError(ValueError):
    """``service.json`` 缺失之外的内容错误。"""


@dataclass(frozen=True, slots=True)
class FeatureToggles:
    market_data: bool = True
    cold_store: bool = True
    alerts: bool = True
    scanner: bool = True

    def enabled_names(self) -> list[str]:
        return [name for name in FEATURE_NAMES if getattr(self, name)]

    def disabled_names(self) -> list[str]:
        return [name for name in FEATURE_NAMES if not getattr(self, name)]


@dataclass(frozen=True, slots=True)
class ServiceConfig:
    features: FeatureToggles
    path: Path
    exists: bool


def service_config_path(data_dir: str | Path) -> Path:
    return Path(data_dir) / SERVICE_CONFIG_NAME


def _error(path: Path, detail: str) -> ServiceConfigError:
    return ServiceConfigError(f"无法解析服务配置 {path}：{detail}")


def load_service_config(data_dir: str | Path) -> ServiceConfig:
    """读取 ``<data-dir>/service.json``；文件缺失时四项功能全部开启。"""

    path = service_config_path(data_dir)
    if not path.exists():
        return ServiceConfig(FeatureToggles(), path, False)

    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise _error(path, f"读取失败：{exc}") from exc

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise _error(path, f"不是合法 JSON（第 {exc.lineno} 行第 {exc.colno} 列）") from exc

    if not isinstance(payload, dict):
        raise _error(path, "顶层必须是 JSON 对象")

    unknown_keys = sorted(payload.keys() - {"version", "features", "comment"})
    if unknown_keys:
        raise _error(
            path,
            f"未知顶层键：{'、'.join(unknown_keys)}。可用键为 version、features、comment",
        )

    version = payload.get("version", CONFIG_VERSION)
    if isinstance(version, bool) or not isinstance(version, int):
        raise _error(path, f"version 必须是整数，收到 {version!r}")
    if version != CONFIG_VERSION:
        raise _error(path, f"不支持的 version={version!r}，仅支持 {CONFIG_VERSION}")

    raw_features = payload.get("features", {})
    if not isinstance(raw_features, dict):
        raise _error(path, "features 必须是 JSON 对象")

    unknown = sorted(set(raw_features) - set(FEATURE_NAMES))
    if unknown:
        available = "、".join(FEATURE_NAMES)
        raise _error(
            path,
            f"未知功能开关：{'、'.join(unknown)}；可用开关：{available}",
        )

    values: dict[str, bool] = {}
    for name in FEATURE_NAMES:
        if name not in raw_features:
            values[name] = True
            continue
        value = raw_features[name]
        if not isinstance(value, bool):
            raise _error(path, f"功能开关 {name} 必须是 true 或 false，收到 {value!r}")
        values[name] = value

    return ServiceConfig(FeatureToggles(**values), path, True)


def describe_features(
    features: FeatureToggles, *, changed_from: FeatureToggles | None = None
) -> str:
    """给 ``talaria up`` / ``talaria service status`` 的一行中文摘要。"""

    return "；".join(
        f"{FEATURE_LABELS[name]}={'开' if getattr(features, name) else '关'}"
        for name in FEATURE_NAMES
        if changed_from is None or getattr(features, name) != getattr(changed_from, name)
    )
