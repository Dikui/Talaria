from __future__ import annotations

import json
from pathlib import Path

import compression.zstd as zstd
import pytest

from talaria.workbench_api.usage_scan import (
    scan_session_file,
    scan_session_traces,
)

pytestmark = pytest.mark.small


def _event(kind: str, **data) -> str:
    return json.dumps({"type": kind, "data": data}, ensure_ascii=False)


def _write_session(root: Path, session_id: str, events: list[str]) -> Path:
    directory = root / "sessions" / "project" / session_id
    directory.mkdir(parents=True)
    path = directory / "session.v4.jsonl.zstd"
    payload = (
        json.dumps({"type": "session", "version": 4, "id": session_id}) + "\n"
        + "\n".join(events)
        + "\n"
    ).encode()
    path.write_bytes(zstd.compress(payload))
    return path


def test_scan_folds_turn_usage_and_keeps_missing_values_none(tmp_path: Path) -> None:
    path = _write_session(
        tmp_path,
        "sess-a",
        [
            _event(
                "assistant/message",
                turn=1,
                step=1,
                usage={"inputTokens": 100, "outputTokens": 10, "cacheReadTokens": 40},
            ),
            _event(
                "assistant/message",
                turn=1,
                step=2,
                usage={"inputTokens": 50, "outputTokens": 5, "cacheReadTokens": 30},
            ),
            _event(
                "assistant/message",
                turn=2,
                step=1,
                usage={"inputTokens": 10, "outputTokens": 2},
            ),
            _event(
                "assistant/message",
                turn=2,
                step=2,
                usage={
                    "inputTokens": 5,
                    "outputTokens": 1,
                    "cacheReadTokens": 3,
                },
            ),
            _event("assistant/message", turn=3, step=1),
        ],
    )

    reports = {report.dsh_turn_id: report for report in scan_session_file(path)}

    assert set(reports) == {"1", "2"}
    assert reports["1"].dsh_session_id == "sess-a"
    assert reports["1"].input_tokens == 150
    assert reports["1"].cached_input_tokens == 70
    assert reports["1"].output_tokens == 15
    assert reports["1"].source == "scan"
    assert reports["2"].input_tokens == 15
    assert reports["2"].cached_input_tokens is None
    assert reports["2"].output_tokens == 3


def test_scan_session_traces_is_idempotent(tmp_path: Path) -> None:
    _write_session(
        tmp_path,
        "sess-a",
        [
            _event(
                "assistant/message",
                turn=1,
                step=1,
                usage={"inputTokens": 7, "outputTokens": 2},
            )
        ],
    )
    seen: set[tuple[str, str]] = set()

    def record(report) -> bool:
        key = (report.dsh_session_id, report.dsh_turn_id)
        if key in seen:
            return False
        seen.add(key)
        return True

    first = scan_session_traces(tmp_path, record)
    second = scan_session_traces(tmp_path, record)

    assert first.scanned_files == 1
    assert first.recorded_turns == 1
    assert first.duplicate_turns == 0
    assert second.recorded_turns == 0
    assert second.duplicate_turns == 1


def test_broken_session_file_is_skipped(tmp_path: Path) -> None:
    directory = tmp_path / "sessions" / "project" / "broken"
    directory.mkdir(parents=True)
    (directory / "session.v4.jsonl.zstd").write_bytes(b"not zstd")

    assert scan_session_file(directory / "session.v4.jsonl.zstd") == []
    result = scan_session_traces(tmp_path, lambda report: True)
    assert result.scanned_files == 1
    assert result.recorded_turns == 0
