"""将磁盘中已经保存的单条件预警转换为 v1 JSON 规则。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from talaria.foundation.rules import Rule

LEGACY_PERIODS = {"EMA": 20, "SMA": 20, "RSI": 14, "ATR": 14, "BOLL": 20, "VWAP": 1}


def _indicator(
    condition: Mapping[str, Any], prefix: str, *, predates_pine: bool,
) -> dict[str, Any]:
    name = condition["indicator" if prefix == "indicator" else "compare_indicator"]
    period = condition.get(f"{prefix}_period")
    seed = condition.get(f"{prefix}_seed")
    params = {"period": LEGACY_PERIODS[name] if period is None else period}
    if name == "VWAP":
        params["anchor"] = condition.get(f"{prefix}_anchor") or "snapshot"
    if name == "BOLL":
        params["std_dev"] = condition.get(f"{prefix}_std_dev") or "2"
    channel = condition.get("indicator_channel") if prefix == "indicator" else None
    if name in {"EMA", "ATR"} and (
        seed == "legacy_sma" or (seed is None and (predates_pine or period is None))
    ):
        name = f"legacy_{name.lower()}"
    return {"source": "indicator", "name": name, "channel": channel, "params": params}


def migrate_rule(condition: Mapping[str, Any], *, predates_pine: bool) -> Rule:
    kind = condition["kind"]
    direction = condition.get("direction")
    test = "gte" if direction == "ABOVE" else "lte"
    constant = {"source": "constant", "value": condition.get("threshold")}
    timing = "event"
    if kind == "METRIC_THRESHOLD":
        expression = {
            "op": "compare", "test": test,
            "left": {"source": "event", "metric": condition["metric"]},
            "right": constant,
        }
    elif kind == "INDICATOR_SIGNAL":
        timing = "bar_close"
        expression = {
            "op": "compare", "test": test,
            "left": _indicator(condition, "indicator", predates_pine=predates_pine),
            "right": (
                _indicator(condition, "compare", predates_pine=predates_pine)
                if condition.get("compare_indicator") is not None else constant
            ),
        }
    elif kind == "OVERLAY_CROSS":
        mode = condition["overlay_mode"]
        timing = "event" if mode == "TOUCH" else "bar_close"
        expression = {
            "op": "overlay", "mode": mode, "direction": direction,
            "drawing_id": condition["drawing_id"], "ratio": condition.get("overlay_ratio"),
        }
    elif kind == "LEVEL_EVENT":
        event = condition["level_event"]
        timing = "event" if event == "TOUCH" else "bar_close"
        expression = {
            "op": "level_event", "level_id": condition["level_id"],
            "level_kind": condition.get("level_kind"), "event": event,
        }
    else:
        raise ValueError(f"未知的历史预警类型：{kind}")
    return Rule.model_validate({"version": "1", "timing": timing, "expression": expression})
