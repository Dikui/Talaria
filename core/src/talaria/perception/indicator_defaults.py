"""工作台和 SDK 共用的指标默认值及只读配置入口。"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from contextlib import closing
from decimal import Decimal
from pathlib import Path

from talaria.foundation.indicators import IndicatorParams
from talaria.foundation.operators import IndicatorName, IndicatorSpec, spec_from_params
from talaria.perception.sqlite import COLLABORATION_STATE_DB

DEFAULTS_KEY = "indicator_defaults.v1"


def effective_defaults(
    overrides: Mapping[IndicatorName, IndicatorParams],
) -> dict[IndicatorName, IndicatorParams]:
    return {name: overrides.get(name) or factory_defaults(name) for name in IndicatorName}


def parse_overrides(raw: object) -> dict[IndicatorName, IndicatorParams]:
    if not isinstance(raw, dict):
        return {}
    overrides: dict[IndicatorName, IndicatorParams] = {}
    for key, value in raw.items():
        try:
            name = IndicatorName(key)
            params = IndicatorParams.model_validate(value)
            spec = spec_from_params(name, params.model_dump(exclude_none=True))
        except (ValueError, TypeError):
            continue
        overrides[name] = params_from_spec(spec)
    return overrides


def factory_defaults(name: IndicatorName) -> IndicatorParams:
    """Pine v6 对齐的工厂默认值。BOLL 倍数显式给出以便前端展示。"""
    params = IndicatorParams.defaults_for(name)
    if name is IndicatorName.BOLL:
        return params.model_copy(update={"std_dev": Decimal("2")})
    return params


def params_from_spec(spec: IndicatorSpec) -> IndicatorParams:
    return IndicatorParams(period=spec.period, std_dev=spec.std_dev, anchor=spec.anchor)


def read_indicator_defaults(data_dir: Path) -> dict[IndicatorName, IndicatorParams]:
    """从协同状态库读出当前生效的参数。尚未初始化时采用工厂默认值。"""
    path = data_dir / COLLABORATION_STATE_DB
    if not path.is_file():
        return effective_defaults({})
    with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)) as connection:
        exists = connection.execute(
            "SELECT 1 FROM sqlite_schema WHERE type = 'table' AND name = 'meta'"
        ).fetchone()
        row = (
            connection.execute("SELECT value FROM meta WHERE key = ?", (DEFAULTS_KEY,)).fetchone()
            if exists else None
        )
    return effective_defaults(parse_overrides(json.loads(row[0]) if row else None))
