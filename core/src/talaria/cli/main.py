"""Talaria 命令行入口：``talaria up --solo`` 与 ``talaria contract-check``。"""

from __future__ import annotations

import argparse
import json
import os
import plistlib
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
import traceback
import urllib.request
import webbrowser
from pathlib import Path
from typing import TYPE_CHECKING, Sequence
from xml.parsers.expat import ExpatError

import uvicorn

from talaria.cli.ticks import add_tick_commands
from talaria.cli.account import add_account_command
from talaria.cli.contracts_io import (
    DSH_TOOL_SPECS,
    check_contracts,
    repo_root,
    write_contracts,
)
from talaria.cli.dsh_host import (
    DEFAULT_DSH_PORT,
    DESKTOP_APP_NAME,
    DESKTOP_PROFILE,
    DshHost,
    DshHostError,
    DshHostInfo,
    bridge_descriptor_path,
    build_install_bundle,
    desktop_installed,
    dsh_entry_path,
    dsh_profile_dir,
    install_talaria_patch,
    profile_has_talaria_patch,
    read_desktop_origin,
    reap_stale_dsh_groups,
    redact_dsh_token,
    remove_bridge_descriptor,
    remove_talaria_patch,
    resolve_dsh_bin,
    write_bridge_descriptor,
)
from talaria.cli.service import (
    DEFAULT_DATA_DIR,
    DEFAULT_SERVICE_LABEL,
    ServiceInstallError,
    ServiceStatus,
    ServiceUninstallError,
    _domain,
    _job_data_dir,
    _launchctl_arguments,
    _launchctl_value,
    _run_launchctl,
    install_service,
    open_data_dir,
    plist_path,
    service_status,
    uninstall_service,
)
from talaria.foundation.errors import ServiceLockError
from talaria.packs import DEFAULT_PACK, MARKET_PACKS, get_market_pack
from talaria.perception.cold_migrate import migrate_cold_store
from talaria.workbench_api.service_config import (
    FeatureToggles,
    ServiceConfigError,
    describe_features,
    load_service_config,
)
from talaria.workbench_api.service_descriptor import (
    ServiceDescriptorError,
    loopback_base_url,
    read_service_descriptor,
    remove_service_descriptor,
    service_descriptor_path,
    validate_loopback_host,
    write_service_descriptor,
)
from talaria.workbench_api.service_lock import (
    ROLE_SERVICE,
    ROLE_USAGE_SCAN,
    ServiceLock,
    probe_lock,
)
from talaria.workbench_api.settings import AppSettings

if TYPE_CHECKING:
    from talaria.foundation.collaboration.contracts import UsageReport
    from talaria.workbench_api.service_descriptor import ServiceDescriptor
    from talaria.workbench_api.usage_scan import UsageScanResult

DEFAULT_PORT = 8765
DEFAULT_DSH_PROFILE = "talaria-desk"


def repo_dist_dir() -> Path:
    return repo_root() / "dsh" / "packages" / "talaria-desk-ui" / "dist"


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


def _cmd_usage_scan(args: argparse.Namespace) -> int:
    """扫描 DSH 会话：服务运行时经其 HTTP 接口写入，否则持同一把锁直写。"""

    from talaria.workbench_api.state_store import StateStore
    from talaria.workbench_api.usage_scan import scan_session_traces

    data_dir = Path(args.data_dir).expanduser().resolve()
    lock = ServiceLock(data_dir, role=ROLE_USAGE_SCAN)
    try:
        lock.acquire()
    except ServiceLockError as exc:
        return _usage_scan_through_service(args, data_dir, str(exc))

    try:
        store = StateStore(data_dir)
        try:
            result = scan_session_traces(args.dsh_home, store.usage.record)
        finally:
            store.close()
    finally:
        lock.release()
    _print_scan_result(result)
    return 0


class _UsageScanServiceError(RuntimeError):
    """经服务接口写入失败；调用方不得回退为直写。"""


def _usage_scan_through_service(
    args: argparse.Namespace, data_dir: Path, lock_error: str
) -> int:
    holder = probe_lock(data_dir)
    if holder is None:
        print(f"无法取得数据目录锁：{lock_error}", file=sys.stderr)
        return 2
    if holder.role != ROLE_SERVICE:
        print(
            f"数据目录正在被另一个进程写入：{data_dir}"
            f"（pid={holder.pid if holder.pid is not None else '未知'}，"
            f"角色={holder.role or '未知'}）。为避免并发写坏协同状态，本次未写入。",
            file=sys.stderr,
        )
        return 2

    try:
        descriptor = read_service_descriptor(data_dir)
    except ServiceDescriptorError as exc:
        print(
            f"Talaria 服务正在运行（pid={holder.pid}），但接口描述不可用：{exc}\n"
            "为避免并发写入，本次未直写数据目录。",
            file=sys.stderr,
        )
        return 2
    if descriptor is None:
        print(
            f"Talaria 服务正在运行（pid={holder.pid}），但尚未发布写入接口"
            f"（{service_descriptor_path(data_dir)} 不存在）；"
            "请等待服务启动完成后重试。",
            file=sys.stderr,
        )
        return 2
    if holder.pid != descriptor.pid:
        print(
            f"数据目录锁由 pid={holder.pid} 持有，但写入接口描述来自 pid={descriptor.pid}；"
            "接口描述可能已过期，本次未直写数据目录。",
            file=sys.stderr,
        )
        return 2
    return _usage_scan_via_http(args, descriptor)


def _usage_scan_via_http(
    args: argparse.Namespace, descriptor: ServiceDescriptor
) -> int:
    from talaria.workbench_api.usage_scan import scan_session_traces

    url = f"{descriptor.base_url}/api/collaboration/usage"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def record(report: UsageReport) -> bool:
        request = urllib.request.Request(
            url,
            data=report.model_dump_json().encode("utf-8"),
            method="POST",
            headers={
                "Authorization": f"Bearer {descriptor.token}",
                "Content-Type": "application/json",
            },
        )
        try:
            with opener.open(request, timeout=5) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (OSError, ValueError) as exc:
            raise _UsageScanServiceError(
                f"经服务写入失败：{exc}\n本次未直写数据目录。"
            ) from exc
        if not isinstance(payload, dict) or not isinstance(
            payload.get("recorded"), bool
        ):
            raise _UsageScanServiceError(
                f"经服务写入失败：响应缺少布尔 recorded 字段（{payload!r}）\n"
                "本次未直写数据目录。"
            )
        return payload["recorded"]

    try:
        result = scan_session_traces(args.dsh_home, record)
    except _UsageScanServiceError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    _print_scan_result(result)
    return 0


def _print_scan_result(result: UsageScanResult) -> None:
    print(
        json.dumps(
            {
                "scanned_files": result.scanned_files,
                "recorded_turns": result.recorded_turns,
                "duplicate_turns": result.duplicate_turns,
                "skipped_turns": result.skipped_turns,
            },
            ensure_ascii=False,
        )
    )


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

    if dual:
        if not 1 <= args.dsh_port <= 65535:
            print(f"DSH 端口必须在 1..65535 之间：{args.dsh_port}", file=sys.stderr)
            return 2
        if args.dsh_port == args.port:
            print("DSH 端口不能与 Talaria 守护进程端口相同", file=sys.stderr)
            return 2

    default_data_dir = os.environ.get("TALARIA_DATA_DIR", str(DEFAULT_DATA_DIR))
    data_dir = Path(
        args.data_dir if args.data_dir is not None else default_data_dir
    ).expanduser().resolve()
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
    if replay_events is not None:
        if args.data_dir is None:
            print("回放模式必须显式指定 --data-dir", file=sys.stderr)
            return 2
        protected_dirs = [Path(default_data_dir).expanduser().resolve(), *_launchd_data_dirs()]
        if any(_data_dirs_overlap(data_dir, protected) for protected in protected_dirs):
            print(f"回放数据目录不能与日常数据目录重叠：{data_dir}", file=sys.stderr)
            return 2
    if args.realtime_quota <= 0:
        print(f"实时订阅配额必须为正数：{args.realtime_quota}", file=sys.stderr)
        return 2

    try:
        service_config = load_service_config(data_dir)
    except ServiceConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    # 服务接口描述与 DSH 桥接凭据都需要 token；solo 模式也生成，
    # 以便同机命令在服务运行时经 HTTP 提交写入。
    token = secrets.token_urlsafe(32)
    settings = AppSettings(
        data_dir=data_dir,
        dist_dir=dist_dir,
        pack=args.pack,
        fixture_dir=fixture_dir,
        replay_events=replay_events,
        replay_speed=args.replay_speed,
        history_base_url=args.history_base_url,
        backfill_bars=args.backfill_bars,
        realtime_quota=args.realtime_quota,
        token=token,
        features=service_config.features,
    )
    service_lock = ServiceLock(data_dir, role=ROLE_SERVICE)
    try:
        service_lock.acquire()
    except ServiceLockError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    try:
        return _run_up(
            args,
            host=host,
            token=token,
            settings=settings,
            features=service_config.features,
            dual=dual,
        )
    finally:
        service_lock.release()


def _data_dirs_overlap(left: Path, right: Path) -> bool:
    """比较已规范化的路径；尚不存在的路径也可能在大小写不敏感的卷上重叠。"""
    folded_left, folded_right = Path(str(left).casefold()), Path(str(right).casefold())
    try:
        same_directory = os.path.samefile(left, right)
    except OSError:
        same_directory = folded_left == folded_right
    return (
        same_directory or folded_left in folded_right.parents or folded_right in folded_left.parents
    )


def _launchd_data_dirs() -> list[Path]:
    """尽力读取当前作业和磁盘 plist，各自解析相对路径。"""
    directories = []
    try:
        result = _run_launchctl(["print", f"{_domain(None)}/{DEFAULT_SERVICE_LABEL}"])
        if result.returncode == 0:
            active = _job_data_dir(
                _launchctl_arguments(result.stdout),
                _launchctl_value(result.stdout, "working directory"),
            )
            if active is not None:
                directories.append(active.resolve())
    except (OSError, RuntimeError):
        pass
    try:
        path = plist_path(Path.home() / "Library" / "LaunchAgents")
        with path.open("rb") as handle:
            config = plistlib.load(handle)
        if isinstance(config, dict):
            arguments = config.get("ProgramArguments", [])
            working_dir = config.get("WorkingDirectory")
            working_dir = working_dir if isinstance(working_dir, str) else None
            if isinstance(arguments, list) and all(isinstance(arg, str) for arg in arguments):
                installed = _job_data_dir(arguments, working_dir)
                if installed is None:
                    env = config.get("EnvironmentVariables")
                    configured = env.get("TALARIA_DATA_DIR") if isinstance(env, dict) else None
                    value = configured if isinstance(configured, str) else str(DEFAULT_DATA_DIR)
                    installed = Path(working_dir or "/") / Path(value).expanduser()
                directories.append(installed.resolve())
    except (OSError, RuntimeError, ValueError, plistlib.InvalidFileException, ExpatError):
        pass
    return directories


def _run_up(
    args: argparse.Namespace,
    *,
    host: str,
    token: str,
    settings: AppSettings,
    features: FeatureToggles,
    dual: bool,
) -> int:
    data_dir = settings.data_dir
    base_url = loopback_base_url(host, args.port)
    bridge_path = bridge_descriptor_path(data_dir)
    descriptor_path = service_descriptor_path(data_dir)
    reap_stale_dsh_groups(data_dir)
    try:
        dsh_entry_path(data_dir).unlink(missing_ok=True)
    except OSError as exc:
        print(redact_dsh_token(f"警告：无法移除旧的 DSH 入口：{exc}"), file=sys.stderr)

    def prepare_bridge() -> None:
        try:
            write_bridge_descriptor(
                bridge_path, base_url=base_url, token=token, daemon_pid=os.getpid()
            )
        except (OSError, ValueError) as exc:
            raise DshHostError(f"无法写入 DSH 桥接凭据：{exc}") from exc

    dsh_host: DshHost | None = None
    if dual:
        dsh_host = DshHost(
            dsh_bin=args.dsh_bin,
            profile=args.dsh_profile,
            host=host,
            port=args.dsh_port,
            bridge_path=bridge_path,
            log_dir=data_dir / "logs",
            startup_timeout=args.dsh_startup_timeout,
            on_offline=_report_host_offline,
            prepare_bridge=prepare_bridge,
        )

    from talaria.workbench_api.app import create_app

    def _collaboration_host() -> str | None:
        if dsh_host is None:
            return read_desktop_origin(data_dir)
        info = dsh_host.info()
        if info.state != "online":
            return None
        # DSH 打印的入口 URL 通常带 ?token=…；桥接派发必须用无查询串基址，
        # 否则拼接 /talaria/consult 会把路径塞进查询串。
        return info.base_url

    def _desktop_collaboration_status() -> dict[str, object]:
        origin = read_desktop_origin(data_dir)
        return {
            "mode": "solo",
            "host": "online" if origin else "offline",
            "url": origin,
            "message": (
                "DSH 桌面应用已连接。"
                if origin else "DSH 桌面应用离线。请打开已安装 Talaria 插件的桌面应用。"
            ),
        }

    collaboration_status = (
        dsh_host.status_payload if dsh_host else _desktop_collaboration_status
    )
    app = create_app(settings, collaboration_status, _collaboration_host)
    server = uvicorn.Server(
        uvicorn.Config(app, host=host, port=args.port, log_level="info")
    )

    # uvicorn 0.54 在优雅关闭后会重新抛出捕获的 SIGTERM，使进程以 -15 退出。
    # solo 守护进程需要可预测的 0 退出码，因此把服务器放到工作线程、由主线程
    # 接收信号并翻转 should_exit；工作线程里的 uvicorn 不会安装信号处理器。
    worker_error: BaseException | None = None
    shutdown_requested = False

    def run_server() -> None:
        nonlocal worker_error
        try:
            server.run()
        except BaseException as exc:
            worker_error = exc

    def stop_server(signum: int, frame: object) -> None:
        nonlocal shutdown_requested
        shutdown_requested = True

    previous_handlers = {
        sig: signal.signal(sig, stop_server)
        for sig in (signal.SIGINT, signal.SIGTERM)
    }
    worker = threading.Thread(target=run_server, name="talaria-uvicorn", daemon=False)
    worker.start()
    startup_failed = False
    try:
        deadline = time.monotonic() + 20
        while (
            not server.started
            and not shutdown_requested
            and worker.is_alive()
            and time.monotonic() < deadline
        ):
            time.sleep(0.05)
        if not server.started and not shutdown_requested:
            startup_failed = True
            print(
                "Talaria 守护进程启动失败；请检查端口占用与日志。",
                file=sys.stderr,
            )
        elif server.started:
            try:
                write_service_descriptor(
                    descriptor_path,
                    base_url=base_url,
                    token=token,
                    pid=os.getpid(),
                    features=features,
                )
            except (OSError, ValueError) as exc:
                # 描述缺失时 usage-scan 会拒绝直写（锁在服务手里），但看盘不受影响。
                print(
                    f"警告：无法发布服务写入接口：{exc}\n"
                    f"同机命令在服务运行期间将无法经服务写入；描述路径：{descriptor_path}",
                    file=sys.stderr,
                )

            try:
                prepare_bridge()
            except DshHostError as exc:
                print(f"警告：{exc}", file=sys.stderr)

            if dsh_host is not None:
                if not shutdown_requested:
                    dsh_host.supervise()
                try:
                    _print_ready(host, args.port, dsh_host.info(), bridge_path, features)
                except OSError:
                    pass

            while worker.is_alive() and not shutdown_requested:
                worker.join(timeout=0.2)
    finally:
        try:
            if dsh_host is not None:
                dsh_host.stop()
        finally:
            server.should_exit = True
            # _cmd_up 退出时会释放数据目录锁，必须先等服务线程关闭存储。
            try:
                remove_bridge_descriptor(bridge_path)
                remove_service_descriptor(descriptor_path)
            finally:
                worker.join()
                for sig, handler in previous_handlers.items():
                    signal.signal(sig, handler)
    if isinstance(worker_error, SystemExit):  # 端口占用等启动失败
        code = worker_error.code
        if isinstance(code, int) and code != 0:
            return code
        if code is not None and not isinstance(code, int):
            print(code, file=sys.stderr)
            return 1
    elif worker_error is not None:
        traceback.print_exception(worker_error)
        return 1
    return 2 if startup_failed else 0


def _report_host_offline(info: DshHostInfo) -> None:
    print(
        f"DSH 协同宿主：离线（{info.detail}）；Talaria 守护进程继续提供图表快通道。\n"
        f"DSH 日志：{info.log_path}",
        file=sys.stderr,
    )


def _print_ready(
    host: str,
    port: int,
    info: DshHostInfo,
    bridge_path: Path,
    features: FeatureToggles,
) -> None:
    print(f"Talaria 看盘：http://{host}:{port}/desk", file=sys.stderr)
    if info.state == "online" and info.url:
        print(f"DSH 协同宿主：{redact_dsh_token(info.url)}", file=sys.stderr)
    elif info.state == "starting":
        print("DSH 协同宿主：启动中", file=sys.stderr)
    else:
        print(f"DSH 协同宿主：离线（{info.detail}）", file=sys.stderr)
    print(f"DSH 日志：{info.log_path}", file=sys.stderr)
    print(f"桥接凭据：{bridge_path}（0600）", file=sys.stderr)
    print(f"功能开关：{describe_features(features)}", file=sys.stderr)


def _electron_managed(result: subprocess.CompletedProcess[str]) -> bool:
    message = "is managed exclusively by the Electron application"
    return result.returncode != 0 and any(
        message in output for output in (result.stderr or "", result.stdout or "")
    )


def _cmd_dsh_install(args: argparse.Namespace) -> int:
    """可重复地把仓库内插件安装到指定的 DSH Profile。"""
    repo = repo_root()
    plugin_dir = repo / "dsh" / "packages" / "talaria-dsh-plugin"
    if not (plugin_dir / "package.json").is_file():
        print(f"插件包不存在：{plugin_dir}", file=sys.stderr)
        return 2
    profile = args.dsh_profile
    desktop = profile.lower() == DESKTOP_PROFILE
    if desktop:
        profile = DESKTOP_PROFILE
    try:
        profile_dir = dsh_profile_dir(profile)
        if desktop and not profile_dir.is_dir():
            print(
                "请先打开 DeepSeek Harness 桌面应用初始化 desktop Profile，退出后再安装。",
                file=sys.stderr,
            )
            return 2
        data_dir = open_data_dir(args.data_dir)
        dsh = None
        if not desktop and not profile_has_talaria_patch(profile_dir):
            dsh = resolve_dsh_bin(args.dsh_bin)
    except (DshHostError, ValueError, OSError) as exc:
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
    try:
        if dsh is not None and not (profile_dir / "package.json").is_file():
            init = subprocess.run(
                [dsh, "--profile", profile, "--from-default-profile", "web", "--dump-config"],
                capture_output=True,
                text=True,
                check=False,
            )
            if _electron_managed(init):
                dsh = None
            else:
                if init.stderr:
                    print(init.stderr, end="", file=sys.stderr)
                if init.returncode != 0:
                    print(
                        f"初始化 DSH Profile 失败：{profile_dir}\n"
                        "请检查 dsh --version 是否为 0.2.2-rc.5。",
                        file=sys.stderr,
                    )
                    return 2
        if dsh is not None:
            bundle = build_install_bundle(
                repo=repo, profile=profile, data_dir=data_dir, tool_names=DSH_TOOL_SPECS
            )
            add = subprocess.run(
                [dsh, "plugin", "--profile", profile, "add", f"file:{bundle}"],
                capture_output=True,
                text=True,
                check=False,
            )
            if _electron_managed(add):
                dsh = None
            else:
                if add.stdout:
                    print(add.stdout, end="")
                if add.stderr:
                    print(add.stderr, end="", file=sys.stderr)
                if add.returncode != 0:
                    print("安装 @talaria/dsh-plugin 失败，请检查 DSH 的安装诊断。", file=sys.stderr)
                    return add.returncode
        if dsh is None:
            install_talaria_patch(profile_dir, plugin_dir, data_dir)
    except (OSError, ValueError) as exc:
        print(f"无法安装 @talaria/dsh-plugin：{exc}", file=sys.stderr)
        return 2
    print(f"已安装 Profile：{profile_dir}", file=sys.stderr)
    return 0


def _cmd_dsh_uninstall(args: argparse.Namespace) -> int:
    profile = args.dsh_profile
    if profile.lower() == DESKTOP_PROFILE:
        profile = DESKTOP_PROFILE
    try:
        profile_dir = dsh_profile_dir(profile)
        removed = remove_talaria_patch(profile_dir)
    except (OSError, ValueError) as exc:
        print(f"无法移除 Talaria 用户补丁：{exc}", file=sys.stderr)
        return 2
    message = "已移除 Talaria 用户补丁" if removed else "未发现 Talaria 用户补丁"
    print(f"{message}：{profile_dir}", file=sys.stderr)
    return 0


def _cmd_cold_migrate(args: argparse.Namespace) -> int:
    from talaria.foundation.errors import ColdStoreError

    pack = get_market_pack(args.pack)
    try:
        report = migrate_cold_store(
            Path(args.data_dir), pack=pack, source_specs=args.source_specs
        )
    except (ColdStoreError, ServiceLockError) as exc:
        print(f"冷库迁移失败：{exc}", file=sys.stderr)
        return 1
    if not report.manifest_switched:
        print(
            f"冷库无需迁移：{report.already_current_generations} 个 generation "
            "已是 decimal128。"
        )
        return 0
    print(
        f"冷库迁移完成：迁移 {report.migrated_generations} 个 generation，"
        f"复用 {report.already_current_generations} 个，"
        f"更新 {report.updated_cursors} 个游标，"
        f"新写 {report.written_files} 个文件。"
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

    pack = get_market_pack(args.pack)
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
        instrument = pack.instrument(config.instrument_id)
        if instrument is None:
            available = "、".join(
                item.instrument_id for item in pack.instruments()
            )
            raise EvaluationError(
                "unknown_instrument",
                f"未知标的: {config.instrument_id}；可用: {available}",
            )
        profile = session_profile_from_trait(pack.traits(config.instrument_id).session)
        fixture_dir = Path(args.fixture_dir) if args.fixture_dir else None
        data_dir = Path(args.data_dir) if args.data_dir else None
        default_fixture_dir = pack.default_fixture_dir
        if fixture_dir is None and data_dir is None and default_fixture_dir is not None:
            fixture_dir = repo_root() / default_fixture_dir
        bars, source = load_compact_bars(
            instrument_symbol=instrument.symbol,
            timeframe=config.timeframe,
            from_ts=config.from_ts,
            to_ts=config.to_ts,
            fixture_dir=fixture_dir,
            data_dir=data_dir,
            raw_archive_id=pack.raw_archive_id,
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


def _default_service_program() -> str | None:
    candidate = Path(sys.executable).with_name("talaria")
    if candidate.is_file():
        return str(candidate.resolve())
    found = shutil.which("talaria")
    return str(Path(found).resolve()) if found is not None else None


def _cmd_service(args: argparse.Namespace) -> int:
    """install/uninstall/status 三个动作；launchctl 只在本机真实执行。"""

    launch_agents_dir = Path(args.launch_agents_dir).expanduser()
    label = args.label
    command = args.service_command

    if command == "install":
        data_dir = Path(args.data_dir).expanduser().resolve()
        program = args.program or _default_service_program()
        if program is None:
            print(
                "未找到 talaria 可执行文件；请用 --program 指定绝对路径。",
                file=sys.stderr,
            )
            return 2
        try:
            result = install_service(
                data_dir=data_dir,
                port=args.port,
                pack=args.pack,
                program=program,
                launch_agents_dir=launch_agents_dir,
                log_dir=args.log_dir,
                label=label,
            )
        except (ServiceConfigError, ServiceInstallError, ValueError, OSError) as exc:
            print(str(exc), file=sys.stderr)
            return 2
        print(f"已安装 launchd Agent：{result.plist_path}")
        print(f"数据目录：{data_dir}")
        print(
            f"功能开关：{describe_features(result.features)}"
            f"（{result.config_path}）"
        )
        print(f"日志：{result.stdout_path} / {result.stderr_path}")
        print("查看状态：talaria service status")
        return 0

    if command == "uninstall":
        try:
            result = uninstall_service(
                launch_agents_dir=launch_agents_dir,
                label=label,
            )
        except (ServiceUninstallError, ValueError, OSError) as exc:
            print(str(exc), file=sys.stderr)
            return 2
        if result.removed:
            print(f"已卸载 launchd Agent：{result.plist_path}")
            print("数据目录与功能开关配置保留。")
        else:
            print(f"未安装：{result.plist_path}")
        return 0

    try:
        status = service_status(
            data_dir=args.data_dir,
            launch_agents_dir=launch_agents_dir,
            label=label,
        )
    except (ServiceConfigError, ServiceDescriptorError, ValueError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    _print_service_status(status)
    return 0


def _print_service_status(status: ServiceStatus) -> None:
    print(f"plist：{status.plist_path}")
    print(f"配置文件：{status.config_path}")
    if status.features is None:
        print("运行态功能开关：不可用")
    else:
        print(f"运行态功能开关：{describe_features(status.features)}")
    if status.disk_features is not None:
        hint = "" if status.config_exists else "（service.json 不存在，默认全开）"
        if status.features is None:
            print(f"磁盘配置：{describe_features(status.disk_features)}{hint}")
        else:
            changes = describe_features(status.disk_features, changed_from=status.features)
            if changes:
                print(f"待重启生效：{changes}{hint}")
    if not status.installed:
        print("状态：未安装（执行 talaria service install 后随登录启动）")
    else:
        state = status.state or ("已加载" if status.loaded else "未知")
        pid_hint = f"，pid={status.pid}" if status.pid is not None else ""
        print(f"状态：已安装，launchd={state}{pid_hint}")
    print(f"代理（plist）：{'，'.join(status.proxy_endpoints) or '未配置'}")
    if status.detail:
        print(f"详情：{status.detail}")
    holder = status.lock_holder
    if holder is None:
        print("数据目录锁：空闲")
    else:
        pid = holder.pid if holder.pid is not None else "未知"
        print(f"数据目录锁：由 {holder.role or '未知角色'} 持有（pid={pid}）")
    print(f"写入接口：{status.endpoint or '不可用'}")
    print(status.dsh.summary)


def _cmd_open(args: argparse.Namespace) -> int:
    try:
        data_dir = open_data_dir(args.data_dir)
        descriptor = None
        deadline = time.monotonic() + 10.0
        while (holder := probe_lock(data_dir)) is not None and holder.role == ROLE_SERVICE:
            candidate = read_service_descriptor(data_dir)
            if candidate is not None and candidate.pid == holder.pid:
                descriptor = candidate
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(0.1, remaining))
    except (ValueError, OSError) as exc:
        print(redact_dsh_token(str(exc)), file=sys.stderr)
        descriptor = None

    if descriptor is None:
        print(
            "常驻服务未就绪。请运行 talaria service install 或 talaria up，"
            "并确认 --data-dir 指向同一数据目录。",
            file=sys.stderr,
        )
        return 2

    if desktop_installed():
        if args.print_action:
            print(f'open -a "{DESKTOP_APP_NAME}"')
            return 0
        try:
            if subprocess.run(["open", "-a", DESKTOP_APP_NAME], check=False).returncode == 0:
                return 0
        except OSError:
            pass
        print("无法打开 DeepSeek Harness 桌面应用，打开兜底页面。", file=sys.stderr)
    else:
        print("未安装 DeepSeek Harness 桌面应用，打开兜底页面。", file=sys.stderr)
    url = f"{descriptor.base_url}/desk"
    if args.print_action:
        print(f"open {url}")
        return 0
    try:
        if webbrowser.open(url):
            return 0
    except (OSError, webbrowser.Error):
        pass
    print("无法打开系统默认浏览器。", file=sys.stderr)
    return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="talaria", description="Talaria 分析工作台")
    subparsers = parser.add_subparsers(dest="command", required=True)
    add_account_command(subparsers)
    add_tick_commands(subparsers)

    up = subparsers.add_parser("up", help="启动本地看盘守护进程")
    up.add_argument(
        "--solo", action="store_true", help="只启动 Python 守护进程，不启动 DSH 宿主"
    )
    up.add_argument("--host", default="127.0.0.1", help="绑定地址，只允许回环 IP")
    up.add_argument("--port", type=int, default=DEFAULT_PORT, help="监听端口")
    up.add_argument(
        "--data-dir",
        default=None,
        help="数据目录（默认 $TALARIA_DATA_DIR 或 ./.talaria，回放时必须显式指定）",
    )
    up.add_argument("--dist-dir", default=None, help="前端构建产物目录")
    up.add_argument("--fixture-dir", default=None, help="离线 fixture 目录")
    up.add_argument(
        "--history-base-url",
        "--bybit-base-url",
        help="覆盖市场包的历史行情 API 基地址",
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

    open_cmd = subparsers.add_parser("open", help="打开 DSH 工作台或看盘兜底页面")
    open_cmd.add_argument(
        "--data-dir",
        help="数据目录（默认取 $TALARIA_DATA_DIR、已安装作业或 ./.talaria）",
    )
    open_cmd.add_argument(
        "--print", dest="print_action", action="store_true", help="只打印将执行的打开操作"
    )
    open_cmd.set_defaults(func=_cmd_open)

    install = subparsers.add_parser(
        "dsh-install", help="构建并安装 @talaria/dsh-plugin 到 DSH Profile"
    )
    install.add_argument("--dsh-bin", help="用于 CLI 安装的 DSH 可执行文件路径")
    install.add_argument(
        "--dsh-profile",
        default=DESKTOP_PROFILE,
        help=f"DSH Profile 名称（默认 {DESKTOP_PROFILE}）",
    )
    install.add_argument(
        "--data-dir", help="数据目录（与 talaria open 的解析顺序相同）"
    )
    install.set_defaults(func=_cmd_dsh_install)

    uninstall = subparsers.add_parser("dsh-uninstall", help="移除 DSH Profile 中的 Talaria 用户补丁")
    uninstall.add_argument(
        "--dsh-profile",
        default=DESKTOP_PROFILE,
        help=f"DSH Profile 名称（默认 {DESKTOP_PROFILE}）",
    )
    uninstall.set_defaults(func=_cmd_dsh_uninstall)

    check = subparsers.add_parser(
        "contract-check", help="检查契约生成物是否零漂移"
    )
    check.add_argument(
        "--write", action="store_true", help="原地重新生成契约文件"
    )
    check.set_defaults(func=_cmd_contract_check)

    usage_scan = subparsers.add_parser(
        "usage-scan", help="扫描 DSH 会话轨迹，补齐 Token 漏报"
    )
    usage_scan.add_argument(
        "--dsh-home",
        default=os.environ.get("DSH_HOME", str(Path.home() / ".dsh")),
        help="DSH 数据目录（默认 $DSH_HOME 或 ~/.dsh）",
    )
    usage_scan.add_argument(
        "--data-dir",
        default=os.environ.get("TALARIA_DATA_DIR", str(DEFAULT_DATA_DIR)),
        help="Talaria 数据目录（默认 $TALARIA_DATA_DIR 或 ./.talaria）",
    )
    usage_scan.set_defaults(func=_cmd_usage_scan)

    evaluate = subparsers.add_parser(
        "evaluate-levels", help="离线评估关键位触及后的 ATR 反应率与随机基线"
    )
    evaluate.add_argument(
        "--instrument", required=True, help="市场包中的标的标识"
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
        "--fixture-dir", default=None, help="离线 fixture 目录"
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

    migrate = subparsers.add_parser(
        "cold-migrate",
        help="把旧字符串冷库 generation 一次性迁移为按列 decimal128",
    )
    migrate.add_argument(
        "--data-dir",
        default=os.environ.get("TALARIA_DATA_DIR", str(DEFAULT_DATA_DIR)),
        help="数据目录（默认 $TALARIA_DATA_DIR 或 ./.talaria）",
    )
    migrate.add_argument(
        "--source-specs",
        type=Path,
        help=(
            "历史来源规格 JSON 文件，键为 venue/symbol/timeframe/旧 generation_id，"
            "值含 tick_size、qty_step（十进制字符串）及 price_precision、qty_precision（整数）"
        ),
    )
    migrate.set_defaults(func=_cmd_cold_migrate)

    service = subparsers.add_parser(
        "service", help="安装、卸载或查看 launchd 常驻服务"
    )
    service_commands = service.add_subparsers(dest="service_command", required=True)
    launch_agents_default = str(Path.home() / "Library" / "LaunchAgents")

    service_install = service_commands.add_parser(
        "install", help="写入 plist 并注册为登录自启的 launchd Agent"
    )
    service_install.add_argument(
        "--data-dir",
        default=os.environ.get("TALARIA_DATA_DIR", str(DEFAULT_DATA_DIR)),
        help="数据目录（默认 $TALARIA_DATA_DIR 或 ./.talaria）",
    )
    service_install.add_argument(
        "--port", type=int, default=DEFAULT_PORT, help="服务监听端口"
    )
    service_install.add_argument(
        "--pack",
        choices=sorted(MARKET_PACKS),
        default=DEFAULT_PACK,
        help=f"市场包（默认 {DEFAULT_PACK}）",
    )
    service_install.add_argument(
        "--program",
        default=None,
        help="talaria 可执行文件绝对路径（默认取当前 Python 同目录的 talaria）",
    )
    service_install.add_argument(
        "--solo", action="store_true", help=argparse.SUPPRESS
    )
    service_install.add_argument(
        "--launch-agents-dir",
        default=launch_agents_default,
        help="LaunchAgents 目录（测试可指向临时目录）",
    )
    service_install.add_argument(
        "--log-dir", default=None, help="日志目录（默认 <data-dir>/logs）"
    )
    service_install.add_argument(
        "--label", default=DEFAULT_SERVICE_LABEL, help="launchd Label"
    )
    service_install.set_defaults(func=_cmd_service)

    service_uninstall = service_commands.add_parser(
        "uninstall", help="停止并移除 launchd Agent（保留数据与配置）"
    )
    service_uninstall.add_argument(
        "--data-dir",
        default=os.environ.get("TALARIA_DATA_DIR", str(DEFAULT_DATA_DIR)),
        help="数据目录（仅用于提示，不删除）",
    )
    service_uninstall.add_argument(
        "--launch-agents-dir",
        default=launch_agents_default,
        help="LaunchAgents 目录",
    )
    service_uninstall.add_argument(
        "--label", default=DEFAULT_SERVICE_LABEL, help="launchd Label"
    )
    service_uninstall.set_defaults(func=_cmd_service)

    service_status_parser = service_commands.add_parser(
        "status", help="查看 launchd 运行状态与已开启功能（不会启动服务）"
    )
    service_status_parser.add_argument(
        "--data-dir",
        default=None,
        help="数据目录（省略时读取作业配置，其次 $TALARIA_DATA_DIR 或 ./.talaria）",
    )
    service_status_parser.add_argument(
        "--launch-agents-dir",
        default=launch_agents_default,
        help="LaunchAgents 目录",
    )
    service_status_parser.add_argument(
        "--label", default=DEFAULT_SERVICE_LABEL, help="launchd Label"
    )
    service_status_parser.set_defaults(func=_cmd_service)

    for command in (up, evaluate, migrate):
        command.add_argument(
            "--pack",
            choices=sorted(MARKET_PACKS),
            default=DEFAULT_PACK,
            help=f"市场包（默认 {DEFAULT_PACK}）",
        )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)
