"""确定性策略批量回放的命令行。"""

import argparse
import os
from pathlib import Path
import sqlite3
import sys

from talaria.foundation.errors import TalariaError


def add_replay_command(subparsers) -> None:
    replay = subparsers.add_parser("replay", help="批量回放确定性策略，输出账本汇总 JSON")
    replay.add_argument("schemes", type=Path, nargs="+", help="使用 parquet_replay 的交易方案 YAML")
    replay.add_argument(
        "--range", dest="windows", nargs=2, type=int, metavar=("START", "END"), action="append", default=[],
        help="UTC 毫秒，首个开盘槽下界与可获取时间上界，可重复。省略时使用方案范围",
    )
    replay.add_argument("--workers", type=int, default=1, help="同时运行的账户进程数（默认 1）")
    replay.add_argument("--data-dir", type=Path, help="数据目录（默认 $TALARIA_DATA_DIR 或 ./.talaria）")
    replay.set_defaults(func=_replay)


def _replay(args: argparse.Namespace) -> int:
    from talaria.orchestration.replay_runner import run_replays

    try:
        report = run_replays(
            args.schemes, args.data_dir or Path(os.environ.get("TALARIA_DATA_DIR") or ".talaria"),
            windows=[(start, end) for start, end in args.windows], workers=args.workers,
        )
    except (OSError, sqlite3.Error, TalariaError, ValueError) as exc:
        print(f"回放失败：{exc}", file=sys.stderr)
        return 2
    print(report.model_dump_json(indent=2))
    return 0
