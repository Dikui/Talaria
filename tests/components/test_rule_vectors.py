"""Cross-language JSON golden vector verification for `talaria.components.rules`."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from talaria.components.rule_vectors import (
    VECTOR_FILENAMES,
    default_vector_dir,
    render_rule_vectors,
    serialize_rule_state,
    _retest_node_paths,
)
from talaria.components.rules import RuleObservation, compile_rule
from talaria.components.runtime import Ctx, RuleState
from talaria.foundation.bars import CompactBar
from talaria.foundation.derivatives import DerivativeKind, DerivativePoint
from talaria.foundation.drawings import DrawingRecord
from talaria.foundation.feed import DerivativeTickEvent, FeedEvent, KlineEvent, OrderBookEvent
from talaria.foundation.key_levels import KeyLevel
from talaria.foundation.rules import Rule
from talaria.foundation.types import decimal_to_string

pytestmark = [pytest.mark.small, pytest.mark.regression]

VECTOR_DIR = default_vector_dir()
VECTOR_PATHS = tuple(VECTOR_DIR / name for name in VECTOR_FILENAMES)


def _parse_event(raw: dict | None) -> FeedEvent | None:
    if raw is None:
        return None
    kind = raw["kind"]
    payload = raw["payload"]
    if kind == "kline":
        return KlineEvent.model_validate(payload)
    if kind == "order_book":
        return OrderBookEvent.model_validate(payload)
    if kind == "derivative_tick":
        return DerivativeTickEvent.model_validate(payload)
    raise ValueError(f"Unknown rule vector event kind: {kind}")


def _assert_observation_matches(obs: RuleObservation | None, expected: dict | None, label: str) -> None:
    if expected is None:
        assert obs is None, f"{label}: expected None, got {obs!r}"
        return
    assert obs is not None, f"{label}: expected {expected!r}, got None"
    assert obs.matched is expected["matched"], label
    assert (None if obs.observed is None else decimal_to_string(obs.observed)) == expected["observed"], label
    assert (None if obs.threshold is None else decimal_to_string(obs.threshold)) == expected["threshold"], label
    assert (None if obs.drawing_kind is None else obs.drawing_kind.value) == expected["drawing_kind"], label
    assert (None if obs.level is None else obs.level.level_id) == expected["level_id"], label
    assert (None if obs.level_event is None else obs.level_event.value) == expected["level_event"], label
    assert obs.level_direction == expected["level_direction"], label
    assert obs.crossed_at == expected["crossed_at"], label


def test_rule_vector_files_are_up_to_date_and_cover_all_categories() -> None:
    expected_files = render_rule_vectors()
    actual_names = sorted(path.name for path in VECTOR_DIR.glob("*.json"))
    assert actual_names == sorted(expected_files)
    for name, expected_content in expected_files.items():
        actual_content = (VECTOR_DIR / name).read_text(encoding="utf-8")
        assert actual_content == expected_content, f"Drift detected in contracts/rule_vectors/{name}"


@pytest.mark.parametrize("path", VECTOR_PATHS, ids=lambda path: path.stem)
def test_rule_golden_vectors_match_python_evaluator(path: Path) -> None:
    suite = json.loads(path.read_text(encoding="utf-8"))
    instrument = suite["instrument_id"]
    timeframe = suite["timeframe"]
    assert suite["cases"], f"{path.name} must contain at least one case"

    for case in suite["cases"]:
        rule = Rule.model_validate(case["rule"])
        compiled = compile_rule(rule)
        node_paths = _retest_node_paths(rule.expression)
        drawings = {
            d["drawing_id"]: DrawingRecord.model_validate(d)
            for d in case["drawings"]
        }
        default_levels = {
            lv["level_id"]: KeyLevel.model_validate(lv)
            for lv in case["levels"]
        }
        deriv_by_kind: dict[DerivativeKind, list[DerivativePoint]] = {}
        for raw_pt in case["derivatives"]:
            pt = DerivativePoint.model_validate(raw_pt)
            deriv_by_kind.setdefault(pt.kind, []).append(pt)

        bars_by_ts: dict[int, CompactBar] = {}
        state = RuleState()
        for step_idx, step in enumerate(case["steps"]):
            for raw_bar in step["upsert_bars"]:
                bar = CompactBar.model_validate(raw_bar)
                bars_by_ts[bar.timestamp] = bar
            confirmed = tuple(bars_by_ts[ts] for ts in sorted(bars_by_ts))
            step_levels = (
                default_levels
                if "levels" not in step
                else {lv["level_id"]: KeyLevel.model_validate(lv) for lv in step["levels"]}
            )
            event = _parse_event(step["event"])
            ctx = Ctx(
                instrument,
                timeframe,
                case["start_ts"],
                step["now_ms"],
                _confirmed_compact=confirmed,
                _bar_ts=step["bar_ts"],
                _indicator_defaults={},
                _read_derivative_revisions=lambda kind: tuple(deriv_by_kind.get(kind, ())),
                drawing=lambda drawing_id: drawings.get(drawing_id),
                levels=step_levels,
                rule_state=state,
            )
            obs = compiled.observe(
                ctx,
                event=event,
                state=state,
                consume_retests=step["consume_retests"],
            )
            label = f"{path.name}:{case['name']}[{step_idx}]"
            _assert_observation_matches(obs, step["expected"], label)
            if "expected_state" in step:
                assert serialize_rule_state(state, node_paths) == step["expected_state"], label
