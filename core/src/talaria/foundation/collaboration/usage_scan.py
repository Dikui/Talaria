"""扫描本地 DSH 会话轨迹，补齐宿主漏报的 Token 用量。

轨迹文件是多个 zstd frame 拼接的 JSONL；每行一个事件。宿主正常在线时逐轮上报，
这里只作为兜底：按 ``(dsh_session_id, dsh_turn_id)`` 折叠 ``assistant/message`` 的
``usage``，任何字段在任一 step 缺失都保持 ``None``，绝不按 0 补齐。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import compression.zstd as zstd

from talaria.foundation.collaboration.contracts import UsageReport

SESSION_FILE_NAME = "session.v4.jsonl.zstd"
_USAGE_FIELDS = ("inputTokens", "cacheReadTokens", "outputTokens")


@dataclass(frozen=True)
class UsageScanResult:
    scanned_files: int = 0
    recorded_turns: int = 0
    duplicate_turns: int = 0
    skipped_turns: int = 0


def _turn_usage(lines: list[str], session_id: str) -> list[UsageReport]:
    totals: dict[int, dict[str, int | None]] = {}
    seen_steps: set[tuple[int, int]] = set()
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") != "assistant/message":
            continue
        data = event.get("data")
        if not isinstance(data, dict):
            continue
        usage = data.get("usage")
        if not isinstance(usage, dict):
            continue
        try:
            turn = int(data["turn"])
        except (KeyError, TypeError, ValueError):
            continue
        step = data.get("step")
        if isinstance(step, int):
            key = (turn, step)
            if key in seen_steps:
                continue
            seen_steps.add(key)
        bucket = totals.setdefault(turn, {name: 0 for name in _USAGE_FIELDS})
        for name in _USAGE_FIELDS:
            value = usage.get(name)
            if bucket[name] is None:
                continue
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                bucket[name] = None
            else:
                bucket[name] = bucket[name] + value  # type: ignore[operator]
    reports: list[UsageReport] = []
    for turn in sorted(totals):
        bucket = totals[turn]
        if all(bucket[name] is None for name in _USAGE_FIELDS):
            continue
        reports.append(
            UsageReport(
                dsh_session_id=session_id,
                dsh_turn_id=str(turn),
                input_tokens=bucket["inputTokens"],
                cached_input_tokens=bucket["cacheReadTokens"],
                output_tokens=bucket["outputTokens"],
                source="scan",
            )
        )
    return reports


def scan_session_file(path: Path) -> list[UsageReport]:
    """读取一个 ``session.v4.jsonl.zstd``；不可读或非会话文件返回空列表。"""

    try:
        raw = zstd.decompress(path.read_bytes())
    except (OSError, zstd.ZstdError):
        return []
    lines = raw.decode("utf-8", errors="replace").splitlines()
    session_id = ""
    for line in lines:
        try:
            header = json.loads(line)
        except json.JSONDecodeError:
            continue
        if header.get("type") == "session" and isinstance(header.get("id"), str):
            session_id = header["id"]
            break
    if not session_id:
        session_id = path.parent.name
    if not session_id:
        return []
    return _turn_usage(lines, session_id)


def scan_session_traces(
    dsh_home: str | Path,
    record: Callable[[UsageReport], bool],
) -> UsageScanResult:
    """扫描 ``dsh_home/sessions`` 下全部轨迹；``record`` 返回 False 表示已存在。"""

    root = Path(dsh_home).expanduser() / "sessions"
    if not root.is_dir():
        return UsageScanResult()
    scanned = recorded = duplicates = skipped = 0
    for path in sorted(root.rglob(SESSION_FILE_NAME)):
        scanned += 1
        for report in scan_session_file(path):
            if record(report):
                recorded += 1
            else:
                duplicates += 1
    return UsageScanResult(
        scanned_files=scanned,
        recorded_turns=recorded,
        duplicate_turns=duplicates,
        skipped_turns=skipped,
    )
