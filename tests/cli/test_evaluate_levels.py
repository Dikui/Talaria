"""`talaria evaluate-levels` 的命令行契约（任务单 12）。"""

from __future__ import annotations

import json

import pytest

from talaria.cli.main import main
from talaria.evaluation.key_levels import registered_level_kinds
from talaria.packs.crypto_perp import CRYPTO_PERP_PACK
from talaria.packs.testing import FixtureConnector
from talaria.perception.raw_store import RawStore

pytestmark = pytest.mark.small

FROM_MS = 1793491200000  # 2026-11-01T00:00:00Z
TO_MS = 1793498400000  # 2026-11-01T02:00:00Z


def _arguments(*extra: str) -> list[str]:
    return [
        "evaluate-levels",
        "--instrument",
        "BTC-USDT-PERP",
        "--timeframe",
        "1m",
        "--from",
        str(FROM_MS),
        "--to",
        str(TO_MS),
        "--baseline-samples",
        "10",
        "--seed",
        "42",
        "--horizon-bars",
        "10",
        *extra,
    ]


def test_evaluate_levels_prints_json_report_by_default(capsys) -> None:
    assert main(_arguments()) == 0
    captured = capsys.readouterr()

    payload = json.loads(captured.out)
    assert payload["instrument_id"] == "BTC-USDT-PERP"
    assert payload["timeframe"] == "1m"
    assert payload["from_ts"] == FROM_MS
    assert payload["to_ts"] == TO_MS
    assert payload["seed"] == 42
    assert payload["atr_period"] == 14
    assert payload["source"].startswith("fixture:")
    assert [row["kind"] for row in payload["results"]] == list(
        registered_level_kinds()
    )
    assert "SWING_HIGH" in captured.err
    assert "total_samples=" in captured.err
    for row in payload["results"]:
        for key in (
            "sample_count",
            "reaction_rate",
            "mean_reaction_atr",
            "baseline_sample_count",
            "baseline_reaction_rate",
            "baseline_mean_reaction_atr",
        ):
            assert key in row


def test_evaluate_levels_accepts_iso_window_in_shanghai(capsys) -> None:
    arguments = [
        "evaluate-levels",
        "--instrument",
        "BTC-USDT-PERP",
        "--timeframe",
        "1m",
        "--from",
        "2026-11-01T08:00:00+08:00",
        "--to",
        "2026-11-01T10:00:00+08:00",
        "--baseline-samples",
        "2",
        "--seed",
        "1",
        "--horizon-bars",
        "10",
        "--json",
    ]

    assert main(arguments) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["from_ts"] == FROM_MS
    assert payload["to_ts"] == TO_MS


def test_evaluate_levels_reports_structured_error_for_unknown_instrument(
    capsys,
) -> None:
    assert main(_arguments("--instrument", "NOPE-USDT-PERP")) == 2
    captured = capsys.readouterr()

    error = json.loads(captured.err)["error"]
    assert error["code"] == "unknown_instrument"
    assert "NOPE-USDT-PERP" in error["message"]
    assert captured.out == ""


def test_evaluate_levels_reports_structured_error_for_unsupported_timeframe(
    capsys,
) -> None:
    assert main(_arguments("--timeframe", "2s")) == 2
    error = json.loads(capsys.readouterr().err)["error"]
    assert error["code"] == "unsupported_timeframe"


# 任务单 04：默认市场包沿用现有原始柱路径。
@pytest.mark.spec
def test_evaluate_levels_reads_existing_crypto_perp_archive(
    tmp_path, fixture_dir, capsys
) -> None:
    raw = FixtureConnector(fixture_dir).fetch_bars(
        symbol="BTCUSDT", timeframe="1m", end_ts=TO_MS, limit=180
    )
    data_dir = tmp_path / "data"
    RawStore(data_dir, raw_archive_id=CRYPTO_PERP_PACK.raw_archive_id).append(
        "BTCUSDT", "1m", raw
    )
    assert (data_dir / "data" / "raw" / "bybit_linear" / "BTCUSDT" / "1m.jsonl").is_file()

    assert (
        main(
            [
                "evaluate-levels",
                "--instrument",
                "BTC-USDT-PERP",
                "--timeframe",
                "1m",
                "--from",
                str(FROM_MS),
                "--to",
                str(TO_MS),
                "--baseline-samples",
                "2",
                "--horizon-bars",
                "10",
                "--data-dir",
                str(data_dir),
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["source"].startswith("raw-store:")


def test_evaluate_levels_reports_structured_error_when_data_is_missing(
    tmp_path, capsys
) -> None:
    assert main(_arguments("--data-dir", str(tmp_path))) == 2

    error = json.loads(capsys.readouterr().err)["error"]
    assert error["code"] == "data_unavailable"


def test_evaluate_levels_prints_table_without_json(capsys) -> None:
    assert main(_arguments("--no-json")) == 0
    captured = capsys.readouterr()

    assert captured.out == ""
    assert "SWING_HIGH" in captured.err
    assert "total_samples=" in captured.err
