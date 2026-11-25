"""回放结果导出与管理命令行：`talaria runs export <run_id>`。"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import sqlite3
import sys

from talaria.foundation.errors import TalariaError


def add_runs_command(subparsers) -> None:
    runs = subparsers.add_parser("runs", help="回放/回测运行产物管理")
    commands = runs.add_subparsers(dest="runs_command", required=True)
    export = commands.add_parser("export", help="从已有运行目录补导 Parquet 投影与 summary.json")
    export.add_argument("run_id", help="运行 ID（对应 <data-dir>/runs/<run_id>）")
    export.add_argument("--data-dir", type=Path, help="数据目录（默认 $TALARIA_DATA_DIR 或 ./.talaria）")
    export.set_defaults(func=_export_run)


def _export_run(args: argparse.Namespace) -> int:
    from talaria.account.run_export import export_run

    data_dir = (args.data_dir or Path(os.environ.get("TALARIA_DATA_DIR") or ".talaria")).expanduser().resolve()
    run_id = args.run_id
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", run_id) or ".." in run_id:
        print(f"无效的运行 ID：{run_id}", file=sys.stderr)
        return 2
    runs_root = (data_dir / "runs").resolve()
    run_dir = (runs_root / run_id).resolve()
    if run_dir.parent != runs_root or not run_dir.is_dir():
        print(f"找不到运行目录：{run_dir}", file=sys.stderr)
        return 2
    try:
        summary = export_run(run_dir, data_dir=data_dir)
    except (FileNotFoundError, OSError, sqlite3.Error, TalariaError, ValueError) as exc:
        print(f"导出回放结果失败：{exc}", file=sys.stderr)
        return 2
    print(summary.model_dump_json(indent=2))
    return 0
