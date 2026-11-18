"""Talaria 命令行入口：``talaria up --solo`` 与 ``talaria contract-check``。"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Sequence

import uvicorn

from talaria.cli.dsh_host import (
    DshHost,
    DshHostError,
    DshHostInfo,
    bridge_descriptor_path,
    remove_bridge_descriptor,
    resolve_dsh_bin,
    write_bridge_descriptor,
)
from talaria.foundation.contracts_io import check_contracts, repo_root, write_contracts
from talaria.workbench_api.settings import AppSettings

DEFAULT_DATA_DIR = Path(".talaria")
DEFAULT_PORT = 8765
DEFAULT_DSH_PORT = 8766
DEFAULT_DSH_PROFILE = "talaria-desk"


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
    dual = not args.solo
    try:
        host = validate_loopback_host(args.host)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if not 1 <= args.port <= 65535:
        print(f"端口必须在 1..65535 之间：{args.port}", file=sys.stderr)
        return 2

    dsh_bin = None
    dsh_start_error: str | None = None
    if dual:
        if not 1 <= args.dsh_port <= 65535:
            print(f"DSH 端口必须在 1..65535 之间：{args.dsh_port}", file=sys.stderr)
            return 2
        if args.dsh_port == args.port:
            print("DSH 端口不能与 Talaria 守护进程端口相同", file=sys.stderr)
            return 2
        try:
            dsh_bin = resolve_dsh_bin(args.dsh_bin)
        except DshHostError as exc:
            # 宿主缺失不应阻塞看盘快通道：守护进程照常启动，仅标记协同离线。
            dsh_start_error = str(exc)
            dsh_bin = args.dsh_bin or "dsh"

    data_dir = Path(args.data_dir).expanduser().resolve()
    fixture_dir = (
        Path(args.fixture_dir).expanduser().resolve() if args.fixture_dir else None
    )
    dist_dir = (
        Path(args.dist_dir).expanduser().resolve()
        if args.dist_dir
        else repo_dist_dir()
    )
    replay_events = None
    if args.replay_events:
        replay_events = Path(args.replay_events).expanduser().resolve()
        if not replay_events.is_file():
            print(f"回放文件不存在：{replay_events}", file=sys.stderr)
            return 2
    if args.replay_speed <= 0:
        print(f"回放速度必须为正数：{args.replay_speed}", file=sys.stderr)
        return 2
    if args.realtime_quota <= 0:
        print(f"实时订阅配额必须为正数：{args.realtime_quota}", file=sys.stderr)
        return 2

    token = secrets.token_urlsafe(32) if dual else None
    settings = AppSettings(
        data_dir=data_dir,
        dist_dir=dist_dir,
        fixture_dir=fixture_dir,
        replay_events=replay_events,
        replay_speed=args.replay_speed,
        bybit_base_url=args.bybit_base_url,
        backfill_bars=args.backfill_bars,
        realtime_quota=args.realtime_quota,
        token=token,
    )
    bridge_path = bridge_descriptor_path(data_dir)
    dsh_host: DshHost | None = None
    if dual:
        assert dsh_bin is not None and token is not None
        dsh_host = DshHost(
            dsh_bin=dsh_bin,
            profile=args.dsh_profile,
            host=host,
            port=args.dsh_port,
            bridge_path=bridge_path,
            log_dir=data_dir / "logs",
            startup_timeout=args.dsh_startup_timeout,
            on_offline=_report_host_offline,
        )
        if dsh_start_error is not None:
            dsh_host.mark_unavailable(dsh_start_error)

    from talaria.workbench_api.app import create_app

    app = create_app(
        settings,
        collaboration_status=dsh_host.status_payload if dsh_host else None,
    )
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
    host_started = False
    try:
        deadline = time.monotonic() + 20
        while (
            not server.started
            and worker.is_alive()
            and time.monotonic() < deadline
        ):
            time.sleep(0.05)
        if not server.started:
            print(
                "Talaria 守护进程启动失败；请检查端口占用与日志。",
                file=sys.stderr,
            )
            return 2 if exit_code == 0 else exit_code

        if dsh_host is not None and token is not None:
            write_bridge_descriptor(
                bridge_path,
                base_url=f"http://{host}:{args.port}",
                token=token,
                daemon_pid=os.getpid(),
            )
            if dsh_start_error is None:
                try:
                    info = dsh_host.start()
                    host_started = True
                except DshHostError as exc:
                    dsh_host.mark_unavailable(str(exc))
                    print(
                        f"警告：{exc}\n"
                        "Talaria 守护进程继续提供图表快通道；"
                        "可修复 DSH 后重启，或使用 --solo 跳过协同宿主。",
                        file=sys.stderr,
                    )
                    info = dsh_host.info()
            else:
                dsh_host.mark_unavailable(dsh_start_error)
                print(
                    f"警告：{dsh_start_error}\n"
                    "Talaria 守护进程继续提供图表快通道；"
                    "修复 DSH 后重启，或使用 --solo 跳过协同宿主。",
                    file=sys.stderr,
                )
                info = dsh_host.info()
            _print_ready(host, args.port, info, bridge_path)

        while worker.is_alive():
            worker.join(timeout=0.2)
    except KeyboardInterrupt:  # pragma: no cover - 主线程信号由 stop_server 接管
        server.should_exit = True
    finally:
        server.should_exit = True
        if host_started and dsh_host is not None:
            dsh_host.stop()
        remove_bridge_descriptor(bridge_path)
        worker.join(timeout=10)
        for sig, handler in previous_handlers.items():
            signal.signal(sig, handler)
    return exit_code


def _report_host_offline(info: DshHostInfo) -> None:
    print(
        f"警告：{info.detail}；Talaria 守护进程继续提供图表快通道。\n"
        f"DSH 日志：{info.log_path}",
        file=sys.stderr,
    )


def _print_ready(host: str, port: int, info: DshHostInfo, bridge_path: Path) -> None:
    print(f"Talaria 看盘：http://{host}:{port}/desk", file=sys.stderr)
    if info.state == "online" and info.url:
        print(f"DSH 协同宿主：{info.url}", file=sys.stderr)
    else:
        print(f"DSH 协同宿主：离线（{info.detail}）", file=sys.stderr)
    print(f"DSH 日志：{info.log_path}", file=sys.stderr)
    print(f"桥接凭据：{bridge_path}（0600）", file=sys.stderr)


def _cmd_dsh_install(args: argparse.Namespace) -> int:
    """可重复地把仓库内插件安装到指定的 DSH Profile。"""
    repo = repo_root()
    plugin_dir = repo / "dsh" / "packages" / "talaria-dsh-plugin"
    if not (plugin_dir / "package.json").is_file():
        print(f"插件包不存在：{plugin_dir}", file=sys.stderr)
        return 2
    canonical_tool = repo / "contracts" / "schemas" / "dsh-tools" / "get_snapshot.tool.json"
    if canonical_tool.is_file():
        shutil.copyfile(canonical_tool, plugin_dir / "get_snapshot.tool.json")
    canonical_patch = repo / "dsh" / "profiles" / "talaria-desk.patch.yml"
    if canonical_patch.is_file():
        shutil.copyfile(canonical_patch, plugin_dir / "talaria-desk.patch.yml")
    try:
        dsh = resolve_dsh_bin(args.dsh_bin)
    except DshHostError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    pnpm = shutil.which("pnpm")
    if pnpm is None:
        print("未找到 pnpm；请先安装 Node.js 24 与 pnpm 11。", file=sys.stderr)
        return 2
    build = subprocess.run(
        [pnpm, "-C", str(repo / "dsh"), "--filter", "@talaria/dsh-plugin", "build"],
        check=False,
    )
    if build.returncode != 0:
        print("DSH 插件构建失败；请修复后重试。", file=sys.stderr)
        return build.returncode
    profile_dir = Path(
        os.environ.get("DSH_HOME", str(Path.home() / ".dsh"))
    ) / "profiles" / args.dsh_profile
    if not profile_dir.is_dir():
        init = subprocess.run(
            [
                dsh,
                "--profile",
                args.dsh_profile,
                "--from-default-profile",
                "web",
                "--dump-config",
            ],
            stdout=subprocess.DEVNULL,
            check=False,
        )
        if init.returncode != 0:
            print(
                f"初始化 DSH Profile 失败：{profile_dir}\n"
                "请检查 dsh --version 是否为 0.2.2-rc.3。",
                file=sys.stderr,
            )
            return 2
    add = subprocess.run(
        [
            dsh,
            "plugin",
            "--profile",
            args.dsh_profile,
            "add",
            f"file:{plugin_dir}",
        ],
        check=False,
    )
    if add.returncode != 0:
        print(
            "安装 @talaria/dsh-plugin 失败。可手动执行：\n"
            f"  dsh plugin --profile {args.dsh_profile} add file:{plugin_dir}",
            file=sys.stderr,
        )
        return add.returncode
    print(
        f"已安装 Profile：{profile_dir}\n"
        f"启动：talaria up --dsh-profile {args.dsh_profile}",
        file=sys.stderr,
    )
    return 0


def _cmd_evaluate_levels(args: argparse.Namespace) -> int:
    from talaria.evaluation.key_levels import (
        EvaluationError,
        KeyLevelEvaluationConfig,
        dumps_report,
        evaluate_key_levels,
        format_table,
        load_compact_bars,
        parse_time_ms,
    )
    from talaria.foundation.errors import FeedUnavailableError
    from talaria.foundation.key_levels import session_profile_from_trait
    from talaria.packs.crypto_perp import CRYPTO_PERP_PACK

    try:
        config = KeyLevelEvaluationConfig(
            instrument_id=args.instrument,
            timeframe=args.timeframe,
            from_ts=parse_time_ms(args.from_ts),
            to_ts=parse_time_ms(args.to_ts),
            atr_multiple=args.atr_multiple,
            horizon_bars=args.horizon_bars,
            baseline_samples=args.baseline_samples,
            seed=args.seed,
            atr_period=args.atr_period,
        )
        instrument = CRYPTO_PERP_PACK.instrument(config.instrument_id)
        if instrument is None:
            available = "、".join(
                item.instrument_id for item in CRYPTO_PERP_PACK.instruments()
            )
            raise EvaluationError(
                "unknown_instrument",
                f"未知标的: {config.instrument_id}；可用: {available}",
            )
        profile = session_profile_from_trait(
            CRYPTO_PERP_PACK.traits(config.instrument_id).session
        )
        fixture_dir = Path(args.fixture_dir) if args.fixture_dir else None
        data_dir = Path(args.data_dir) if args.data_dir else None
        if fixture_dir is None and data_dir is None:
            fixture_dir = repo_root() / "tests" / "fixtures" / "bybit"
        bars, source = load_compact_bars(
            instrument_symbol=instrument.symbol,
            timeframe=config.timeframe,
            from_ts=config.from_ts,
            to_ts=config.to_ts,
            fixture_dir=fixture_dir,
            data_dir=data_dir,
            horizon_bars=config.horizon_bars,
            atr_period=config.atr_period,
        )
        report = evaluate_key_levels(bars, config, session_profile=profile)
    except FeedUnavailableError as exc:
        print(
            json.dumps(
                {"error": {"code": "data_unavailable", "message": str(exc)}},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2
    except EvaluationError as exc:
        print(
            json.dumps(
                {"error": {"code": exc.code, "message": exc.message}},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2

    if args.json:
        print(dumps_report(report, source=source))
    print(format_table(report), file=sys.stderr)
    print(f"source={source}", file=sys.stderr)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="talaria", description="Talaria 分析工作台")
    subparsers = parser.add_subparsers(dest="command", required=True)

    up = subparsers.add_parser("up", help="启动本地看盘守护进程")
    up.add_argument(
        "--solo", action="store_true", help="只启动 Python 守护进程，不启动 DSH 宿主"
    )
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
    up.add_argument(
        "--realtime-quota",
        type=int,
        default=8,
        help="实时订阅配额（当前图表 + 自选；默认 8）",
    )
    up.add_argument(
        "--replay-events",
        default=None,
        help="回放 JSONL 脚本；与 --fixture-dir 并用时 fixture 只供历史快照",
    )
    up.add_argument(
        "--replay-speed",
        type=float,
        default=1.0,
        help="回放速度倍率（默认 1.0，必须为正数）",
    )
    up.add_argument(
        "--dsh-bin",
        default=os.environ.get("TALARIA_DSH_BIN"),
        help="DSH 可执行文件路径（默认从 PATH 查找 dsh）",
    )
    up.add_argument(
        "--dsh-port",
        type=int,
        default=DEFAULT_DSH_PORT,
        help=f"DSH Web 端口（默认 {DEFAULT_DSH_PORT}）",
    )
    up.add_argument(
        "--dsh-profile",
        default=DEFAULT_DSH_PROFILE,
        help=f"DSH Profile 名称（默认 {DEFAULT_DSH_PROFILE}）",
    )
    up.add_argument(
        "--dsh-startup-timeout",
        type=float,
        default=40.0,
        help="DSH 启动与健康探针超时秒数（默认 40）",
    )
    up.set_defaults(func=_cmd_up)

    install = subparsers.add_parser(
        "dsh-install", help="构建并安装 @talaria/dsh-plugin 到 DSH Profile"
    )
    install.add_argument(
        "--dsh-bin",
        default=os.environ.get("TALARIA_DSH_BIN"),
        help="DSH 可执行文件路径",
    )
    install.add_argument(
        "--dsh-profile",
        default=DEFAULT_DSH_PROFILE,
        help=f"DSH Profile 名称（默认 {DEFAULT_DSH_PROFILE}）",
    )
    install.set_defaults(func=_cmd_dsh_install)

    check = subparsers.add_parser(
        "contract-check", help="检查契约生成物是否零漂移"
    )
    check.add_argument(
        "--write", action="store_true", help="原地重新生成契约文件"
    )
    check.set_defaults(func=_cmd_contract_check)

    evaluate = subparsers.add_parser(
        "evaluate-levels", help="离线评估关键位触及后的 ATR 反应率与随机基线"
    )
    evaluate.add_argument(
        "--instrument", required=True, help="标的，例如 BTC-USDT-PERP"
    )
    evaluate.add_argument(
        "--timeframe", required=True, help="周期，例如 1m/5m/1h"
    )
    evaluate.add_argument(
        "--from",
        dest="from_ts",
        required=True,
        help="起始时间（毫秒时间戳或 ISO-8601；无时区按 Asia/Shanghai）",
    )
    evaluate.add_argument(
        "--to",
        dest="to_ts",
        required=True,
        help="结束时间（毫秒时间戳或 ISO-8601；无时区按 Asia/Shanghai）",
    )
    evaluate.add_argument(
        "--atr-multiple",
        type=float,
        default=1.0,
        help="反应阈值，单位为 ATR 倍数（默认 1.0，ATR 周期见 --atr-period）",
    )
    evaluate.add_argument(
        "--horizon-bars",
        type=int,
        default=20,
        help="触及后观察的柱数（默认 20）",
    )
    evaluate.add_argument(
        "--baseline-samples",
        type=int,
        default=200,
        help="每个类别的随机价位样本数（默认 200）",
    )
    evaluate.add_argument(
        "--seed", type=int, default=42, help="随机基线种子（默认 42）"
    )
    evaluate.add_argument(
        "--atr-period", type=int, default=14, help="ATR 周期（默认 14）"
    )
    evaluate.add_argument(
        "--fixture-dir", default=None, help="离线 Bybit fixture 目录"
    )
    evaluate.add_argument(
        "--data-dir",
        default=None,
        help="RawStore 数据目录；未给 --fixture-dir 时读取其中的历史柱",
    )
    output = evaluate.add_mutually_exclusive_group()
    output.add_argument(
        "--json",
        dest="json",
        action="store_true",
        default=True,
        help="向 stdout 输出 JSON，同时向 stderr 输出可读表格（默认）",
    )
    output.add_argument(
        "--no-json", dest="json", action="store_false", help="只输出可读表格到 stderr"
    )
    evaluate.set_defaults(func=_cmd_evaluate_levels)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)
