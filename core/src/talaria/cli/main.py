"""Talaria 命令行入口：``talaria up --solo`` 与 ``talaria contract-check``。"""

from __future__ import annotations

import argparse
import ipaddress
import os
import signal
import sys
import threading
from pathlib import Path
from typing import Sequence

import uvicorn

from talaria.foundation.contracts_io import check_contracts, repo_root, write_contracts
from talaria.workbench_api.settings import AppSettings

DEFAULT_DATA_DIR = Path(".talaria")
DEFAULT_PORT = 8765


def repo_dist_dir() -> Path:
    return repo_root() / "dsh" / "packages" / "talaria-desk-ui" / "dist"


def validate_loopback_host(host: str) -> str:
    """只允许回环地址；``--solo`` 不对外暴露端口。"""
    try:
        address = ipaddress.ip_address(host)
    except ValueError as exc:
        raise ValueError(f"host 必须是回环 IP 地址：{host}") from exc
    if not address.is_loopback:
        raise ValueError(f"--solo 只允许绑定回环地址，拒绝：{host}")
    return host


def _cmd_contract_check(args: argparse.Namespace) -> int:
    if args.write:
        written = write_contracts()
        for path in written:
            print(f"已生成：{path}")
        problems = check_contracts()
        if problems:
            print("生成后自检失败：", file=sys.stderr)
            for problem in problems:
                print(problem, file=sys.stderr)
            return 1
        return 0
    problems = check_contracts()
    if problems:
        print("契约生成物与代码不一致：", file=sys.stderr)
        for problem in problems:
            print(problem, file=sys.stderr)
        return 1
    print("契约生成物零漂移。")
    return 0


def _cmd_up(args: argparse.Namespace) -> int:
    if not args.solo:
        print(
            "DSH 协同模式将在 M2 提供，请使用 --solo",
            file=sys.stderr,
        )
        return 2
    try:
        host = validate_loopback_host(args.host)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if not 1 <= args.port <= 65535:
        print(f"端口必须在 1..65535 之间：{args.port}", file=sys.stderr)
        return 2

    data_dir = Path(args.data_dir).expanduser().resolve()
    fixture_dir = (
        Path(args.fixture_dir).expanduser().resolve() if args.fixture_dir else None
    )
    dist_dir = (
        Path(args.dist_dir).expanduser().resolve()
        if args.dist_dir
        else repo_dist_dir()
    )
    settings = AppSettings(
        data_dir=data_dir,
        dist_dir=dist_dir,
        fixture_dir=fixture_dir,
        bybit_base_url=args.bybit_base_url,
        backfill_bars=args.backfill_bars,
    )
    from talaria.workbench_api.app import create_app

    app = create_app(settings)
    server = uvicorn.Server(
        uvicorn.Config(app, host=host, port=args.port, log_level="info")
    )

    # uvicorn 0.54 在优雅关闭后会重新抛出捕获的 SIGTERM，使进程以 -15 退出。
    # solo 守护进程需要可预测的 0 退出码，因此把服务器放到工作线程、由主线程
    # 接收信号并翻转 should_exit；工作线程里的 uvicorn 不会安装信号处理器。
    exit_code = 0

    def run_server() -> None:
        nonlocal exit_code
        try:
            server.run()
        except SystemExit as exc:  # 端口占用等启动失败
            exit_code = int(exc.code or 0)

    def stop_server(signum: int, frame: object) -> None:
        server.should_exit = True

    previous_handlers = {
        sig: signal.signal(sig, stop_server)
        for sig in (signal.SIGINT, signal.SIGTERM)
    }
    worker = threading.Thread(target=run_server, name="talaria-uvicorn", daemon=False)
    worker.start()
    try:
        while worker.is_alive():
            worker.join(timeout=0.2)
    except KeyboardInterrupt:  # pragma: no cover - 主线程信号由 stop_server 接管
        server.should_exit = True
    finally:
        server.should_exit = True
        worker.join(timeout=10)
        for sig, handler in previous_handlers.items():
            signal.signal(sig, handler)
    return exit_code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="talaria", description="Talaria 分析工作台")
    subparsers = parser.add_subparsers(dest="command", required=True)

    up = subparsers.add_parser("up", help="启动本地看盘守护进程")
    up.add_argument("--solo", action="store_true", help="单机模式（M1 唯一支持的模式）")
    up.add_argument("--host", default="127.0.0.1", help="绑定地址，只允许回环 IP")
    up.add_argument("--port", type=int, default=DEFAULT_PORT, help="监听端口")
    up.add_argument(
        "--data-dir",
        default=os.environ.get("TALARIA_DATA_DIR", str(DEFAULT_DATA_DIR)),
        help="数据目录（默认 $TALARIA_DATA_DIR 或 ./.talaria）",
    )
    up.add_argument("--dist-dir", default=None, help="前端构建产物目录")
    up.add_argument("--fixture-dir", default=None, help="离线 Bybit fixture 目录")
    up.add_argument(
        "--bybit-base-url",
        default="https://api.bybit.com",
        help="Bybit API 基地址",
    )
    up.add_argument(
        "--backfill-bars",
        type=int,
        default=3000,
        help="首次回填目标根数（默认 3000）",
    )
    up.set_defaults(func=_cmd_up)

    check = subparsers.add_parser(
        "contract-check", help="检查契约生成物是否零漂移"
    )
    check.add_argument(
        "--write", action="store_true", help="原地重新生成契约文件"
    )
    check.set_defaults(func=_cmd_contract_check)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)
