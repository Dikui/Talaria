"""账户进程命令行。"""

import argparse
import os
import sqlite3
import sys
from pathlib import Path

from talaria.foundation.errors import TalariaError


def add_account_command(subparsers) -> None:
    account = subparsers.add_parser("account", help="运行交易方案")
    commands = account.add_subparsers(dest="account_command", required=True)
    run = commands.add_parser("run", help="启动账户进程")
    run.add_argument("scheme", type=Path, help="交易方案 YAML")
    run.add_argument("--mode", choices=("backtest",), required=True)
    run.add_argument("--data-dir", type=Path, help="数据目录（默认 $TALARIA_DATA_DIR 或 ./.talaria）")
    run.set_defaults(func=_run_account)


def _run_account(args: argparse.Namespace) -> int:
    from talaria.account.host import run_backtest

    data_dir = args.data_dir or Path(os.environ.get("TALARIA_DATA_DIR") or ".talaria")
    try:
        run = run_backtest(args.scheme, data_dir)
    except (OSError, sqlite3.Error, TalariaError, ValueError) as exc:
        print(f"账户进程失败：{exc}", file=sys.stderr)
        return 2
    print(run.directory)
    return 0
