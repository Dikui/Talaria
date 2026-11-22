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
    run.add_argument("scheme", type=Path, help="交易方案 YAML，或使用 --replay 传入 run-config.json")
    run.add_argument("--mode", choices=("backtest", "paper"), required=True)
    run.add_argument("--data-dir", type=Path, help="数据目录（默认 $TALARIA_DATA_DIR 或 ./.talaria）")
    run.add_argument("--replay", action="store_true", help="使用已有回测产物中固定的交易方案和指标默认值")
    run.set_defaults(func=_run_account)

    register = commands.add_parser("strategy-register", help="由交易员确认 Python 文件插件的来源并登记代码快照哈希")
    approve = commands.add_parser("strategy-approve", help="批准已审阅的 agent 策略代码哈希")
    for command in (register, approve):
        command.add_argument("path", type=Path, help="策略或宿主插件的 Python 入口文件")
        command.add_argument("--data-dir", type=Path, help="数据目录（默认 $TALARIA_DATA_DIR 或 ./.talaria）")
        command.set_defaults(func=_manage_strategy_code)
    register.add_argument("--source", choices=("human", "agent"), required=True)
    approve.add_argument("--sha256", required=True, help="交易员审阅的完整策略快照 SHA-256（由 strategy-register 输出）")
    approve.add_argument("--approved-by", required=True, help="批准人")


def _data_dir() -> Path:
    return Path(os.environ.get("TALARIA_DATA_DIR") or ".talaria")


def _manage_strategy_code(args: argparse.Namespace) -> int:
    from talaria.account.strategy_code import StrategyCodeRegistry

    registry = StrategyCodeRegistry(args.data_dir or _data_dir())
    try:
        record = (
            registry.register(args.path, args.source) if args.account_command == "strategy-register"
            else registry.approve(args.path, args.sha256, args.approved_by)
        )
    except (OSError, sqlite3.Error, ValueError) as exc:
        print(f"策略代码登记失败：{exc}", file=sys.stderr)
        return 2
    print(record.model_dump_json())
    return 0


def _run_account(args: argparse.Namespace) -> int:
    from talaria.account.host import run_backtest, run_paper
    from talaria.account.scheme import SchemeError

    data_dir = args.data_dir
    if data_dir is None and not args.replay:
        data_dir = _data_dir()
    try:
        if args.mode == "paper":
            if args.replay:
                raise SchemeError("--replay 仅用于回测")
            try:
                run_paper(args.scheme, data_dir)
            except KeyboardInterrupt:
                pass
            return 0
        run = run_backtest(args.scheme, data_dir, replay=args.replay)
    except (OSError, sqlite3.Error, TalariaError, ValueError) as exc:
        print(f"账户进程失败：{exc}", file=sys.stderr)
        return 2
    print(run.directory)
    return 0
