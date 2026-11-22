# 01 — launchd 常驻服务与功能开关

**What to build:** `talaria service install` 后，服务随登录启动、崩溃自动重启，关掉所有页面后 Notebook 仍能取数；`talaria service status` 显示运行状态与已开启的功能；行情、冷库写入、预警、扫描可以按配置单独开关。

**Blocked by:** None — can start immediately.

**Source of truth:** ADR-0014；`docs/architecture.md` §1.1、§8.1。

**Status:** resolved

- [x] `install | uninstall | status` 三个子命令；plist 显式设置 `ProcessType` 与 `KeepAlive`，日志写入数据目录。
- [x] 功能开关配置放在数据目录；关闭的功能不启动对应引擎，也不写对应存储。
- [x] `talaria up` 前台模式保留。
- [x] 服务启动时取得数据目录的排他锁（`runtime/service.lock`），同一数据目录第二个实例启动失败并说明原因。
- [x] `talaria usage-scan` 不再另开写连接：服务运行时经服务接口提交，服务未运行时先取得同一把锁再写。
- [ ] 测试：配置解析与各开关组合下组合根装配的引擎；plist 生成内容快照；两个进程争用同一数据目录时第二个失败；服务运行时 `usage-scan` 的写入经服务落库。launchd 实际安装需在本机手动验证，在验收说明中记录步骤与结果。

## Comments

### 实现记录（platform-01）

- **launchd**：新增 `core/src/talaria/cli/service.py`。`render_launchd_plist` 是纯函数，plist 显式包含 `Label`、`ProgramArguments`、`RunAtLoad`、`KeepAlive`（`SuccessfulExit=false`）、`ProcessType=Interactive`、`ThrottleInterval=10`、`WorkingDirectory`、`StandardOutPath`、`StandardErrorPath`；日志默认写到 `<data-dir>/logs/service.{out,err}.log`。`install_service`/`uninstall_service`/`service_status` 的 `launchctl` 调用通过 `runner` 注入，测试用 fake runner，不执行真实 `launchctl`，也不写真实 `~/Library/LaunchAgents`。CLI 新增 `talaria service install|uninstall|status`，集中在 `_cmd_service`；`service status` 不启动服务。
- **功能开关**：新增 `<data-dir>/service.json`（`service_config.py`），四项 `market_data`/`cold_store`/`alerts`/`scanner`，缺省全开；`install` 首次写入全开配置且不覆盖已有文件。顶层接受 `version`、`features` 和说明字段 `comment`，未知顶层键、未知功能开关、非 bool、坏 JSON、版本不符均报出配置路径。`AppSettings.features` 传入组合根 `build_runtime`：关闭行情采集不建实时流/`FeedSupervisor`/热缓冲/订阅调度；关闭冷库写入不建 `ColdStore` 与 `ColdPublisher`；关闭预警不建 `AlertService`/ticker/listener；关闭扫描不建 `ScannerService`/ticker/listener。关闭功能的 HTTP 写入口返回 `409 FEATURE_DISABLED`（`Runtime.require_alerts/require_scanner`），不启动对应引擎、不写对应存储。
- **排他锁**：新增 `<data-dir>/runtime/service.lock`（`service_lock.py`），`fcntl.flock(LOCK_EX|LOCK_NB)`；锁文件内记录 pid/角色/启动时间；第二个实例失败信息包含数据目录、锁路径、持有者 pid/角色。`talaria up`（含 `--solo`）启动时取锁，`finally` 释放；锁文件保留不删，避免删除/重建 inode 造成双写。服务启动成功后写 0600 的 `<data-dir>/runtime/service-descriptor.json`（回环 base_url、token、pid、features），退出时删除。
- **usage-scan**：先尝试取同一把锁；取到则直写 `StateStore`；取不到时探测持有者：若为 `service` 且描述文件 pid 与持有者一致，则逐条 POST `/api/collaboration/usage`（Bearer token 来自描述文件）；服务在启动窗口内描述文件缺失、描述过期、或 HTTP 失败时一律以退出码 2 失败关闭（明确提示“未直写数据目录”），不打开 `StateStore`。其他角色持锁时提示“另一个进程”及 pid，不写入。
- **前台模式**：`talaria up` 双进程前台模式保留；DSH 仍由前台 `up` 启动，launchd Agent 只跑 `up --solo`。服务运行时 `service status` 可显示 launchd state/pid、功能开关、锁持有者与写入接口；未安装时只报告状态，不启动服务。

### 实际运行的命令与结果

- `.venv/bin/pytest tests/workbench_api -q` → `210 passed, 1 warning`（Starlette/httpx deprecation）。
- `.venv/bin/pytest tests/cli -q` → `67 passed, 1 failed`。失败为 `tests/cli/test_talaria_desk_profile.py::test_real_dump_config_matches_permission_snapshot`：本机已安装的 `talaria-desk` DSH profile 文案与仓库快照不一致（`preset-talaria-desk` 的“预警草案” vs “图表协同”），本分支未改 `dsh/` 与该测试，属环境差异。
- 新增/受影响测试：`.venv/bin/pytest tests/workbench_api/test_service_config.py tests/workbench_api/test_service_lock.py tests/workbench_api/test_runtime_features.py tests/cli/test_service.py tests/cli/test_usage_scan_service.py -q` → `44 passed`；`.venv/bin/pytest tests/workbench_api/test_runtime.py tests/workbench_api/test_usage_scan.py tests/workbench_api/test_app.py tests/workbench_api/test_collaboration_api.py tests/cli/test_cli.py tests/cli/test_graceful_shutdown.py -q` → `59 passed`。
- 手工冒烟（临时数据目录、未执行真实 launchctl）：`up --solo --port 18876 --fixture-dir tests/fixtures/bybit` → `/api/health` 200；`service-descriptor.json` 的 version/base_url/pid/features 正确；同数据目录第二个 `up` 退出码 2，stderr 含数据目录、锁文件、`持有者：pid=…，角色=service` 与“同一数据目录只允许一个写者”；`service status` 显示未安装、四项功能全开、锁由 service 持有及写入接口；SIGTERM 后 `up` 退出码 0，`status` 显示锁空闲、接口不可用。

### 未验证项与风险

- **未执行真实 launchd 安装**（按要求不执行 `launchctl bootstrap`、不写 `~/Library/LaunchAgents`）。真实登录自启、崩溃重启、plist 被 launchd 接受与否只在人工步骤中验证。
- `service.json` 只在服务/`up` 启动时读取；修改后需重启服务生效（`service status` 会显示文件当前内容）。
- 服务进程是 `up --solo`，不代管 DSH；需要 DSH 协同宿主时仍用 `talaria up` 双进程模式。
- `uninstall` 会先 `bootout` 再删除 plist；若 launchd 因权限拒绝 bootout，plist 仍会被移除，已加载的作业会保留到注销/重启。若需要严格失败语义，后续可按 launchctl 退出码区分“未加载”和“拒绝操作”。
- `usage-scan` 在服务启动但描述文件尚未写出的极短窗口内会失败关闭（退出码 2），不会直写；这是刻意的安全选择。
- `talaria up` 抢锁可能影响原有“同一数据目录同时跑多个前台实例”的用法；这是本单要求的排他写者语义，现有测试已通过。

### launchd 人工验证步骤（本机执行，未在本次自动化中执行）

1. 在 worktree 根目录执行：
   `.venv/bin/talaria service install --data-dir "$HOME/.talaria" --port 8765`
   预期输出：`已安装 launchd Agent：$HOME/Library/LaunchAgents/com.talaria.workbench.plist`，并打印数据目录、功能开关、日志路径与 `talaria service status` 提示。用 `launchctl print gui/$(id -u)/com.talaria.workbench` 应看到 `state = running`、`pid = ...`。
2. 执行 `.venv/bin/talaria service status`。预期：`状态：已安装，launchd=running，pid=...`；`功能开关：行情采集=开；冷库写入=开；预警=开；扫描=开`；`数据目录锁：由 service 持有（pid=...）`；`写入接口：http://127.0.0.1:8765`。访问 `http://127.0.0.1:8765/api/health` 返回 200。
3. 执行 `kill -9 $(launchctl print gui/$(id -u)/com.talaria.workbench | awk '/pid =/{print $3; exit}')`，等待约 10–15 秒（`ThrottleInterval` 10 秒）。再次 `talaria service status`，预期 launchd 与锁持有者变成新的 pid，`/api/health` 恢复 200；`~/.talaria/logs/service.err.log` 不应出现持续崩溃循环。此步验证 `KeepAlive` 崩溃自动重启。
4. 执行 `.venv/bin/talaria service uninstall`。预期：`已卸载 launchd Agent：...plist`，提示保留数据与配置；`launchctl print` 返回找不到作业，`talaria service status` 显示 `状态：未安装`、`数据目录锁：空闲`、`写入接口：不可用`；端口不再监听，`~/Library/LaunchAgents/com.talaria.workbench.plist` 不存在。
5. 可选：服务运行时执行 `.venv/bin/talaria usage-scan --data-dir "$HOME/.talaria"`，预期输出 `recorded_turns/duplicate_turns/...`，且 `GET /api/collaboration/usage` 汇总 turns 增加；该路径应经 service descriptor 的 HTTP 接口写入，而非直连状态库。

> 说明：第 1 步会真实写入 `~/Library/LaunchAgents` 并注册 launchd 作业；本次按任务边界未执行，以上为需要人工在本机完成的验收步骤。

**合并记录（2026-11-22）**

- 并入 `integration/platform-redesign-batch-1`（merge commit `623b04b`）。
- 合并后复跑 `pytest tests/workbench_api -q` → 210 passed；新增/受影响测试 44 passed；综合受影响范围 585 passed（`--deselect` 既有环境相关失败）。
- 第 16 项保留未勾选：真实 launchd 安装、崩溃重启与卸载需在本机人工执行（步骤见上），其自动化部分已通过。

### 验收返工（2026-11-22）

| 问题 | 确认与修复 | 回归测试及结果 |
|---|---|---|
| 1. 启动超时提前解锁 | 复现工作线程未结束时锁已空闲。清理路径等待服务线程完成存储关闭后再释放数据目录锁。 | `tests/cli/test_graceful_shutdown.py::test_up_keeps_lock_until_timed_out_worker_finishes`，红 1 → 绿 1。 |
| 2. 服务线程崩溃退出 0 | 复现服务就绪后抛出 `RuntimeError` 仍返回 0。主线程接收异常、输出诊断并返回非零。 | `tests/cli/test_graceful_shutdown.py::test_up_returns_nonzero_when_a_started_worker_crashes`，红 1 → 绿 1。 |
| 3. 卸载失败仍删除 plist | 复现权限或 I/O 错误后仍报告成功。作业未加载（`ESRCH`）可清理 plist，其他失败保留 plist 并退出 2。 | `tests/cli/test_service.py::test_uninstall_cli_preserves_plist_on_bootout_failure`，红 2 → 绿 3。 |
| 4. 状态目录和功能开关取错 | 复现省略参数后探测默认目录。保留显式 `--data-dir`，否则优先取已加载作业的参数，缺失时读 plist，然后才使用环境变量或默认目录。描述文件的 PID 与服务锁持有者匹配时显示运行态，另列磁盘差异。 | `tests/cli/test_service.py::test_status_reports_running_service_and_features`、`::test_status_cli_reports_matching_runtime_and_pending_changes`，红 3 → 绿 3。 |
| 5. 关闭行情后仍回填 | 复现快照追加 RawStore 和自选向历史连接器取报价。关闭行情采集后仅从已有 RawStore K 线取历史，允许快照使用现有根数。缺少快照所需周期时返回 `409 FEATURE_DISABLED`，缺少自选报价时返回 `UNAVAILABLE`。 | `tests/workbench_api/test_runtime_features.py::test_market_data_disabled_snapshot_uses_only_local_history`、`::test_market_data_disabled_watchlist_quotes_use_only_local_history`，红 4 → 绿 4。 |
| 6. 回环地址校验不一致 | 复现接口描述拒绝 `127.0.0.2`，CLI 不规范化展开的 IPv6。启动与描述文件共用回环 IP 校验、规范化和 URL 格式，描述文件兼容原有 `localhost`。 | `tests/cli/test_cli.py::test_up_loopback_host_can_be_published_in_service_descriptor`，红 2 → 绿 2。 |
| 7. 顶层误拼静默全开 | 复现 `feature` 等误拼生效为全开。配置文件遇到未知顶层键时报出路径、误拼键及合法键，保留 `comment` 说明字段。 | `tests/workbench_api/test_service_config.py::test_unknown_top_level_key_reports_expected_keys`，红 4 → 绿 4，`::test_config_allows_comment_metadata` 通过。 |
| 8. 关闭扫描后仍可更新简报 | 复现既有简报可标记已读和提交结果。任务单要求关闭的功能停止写入对应存储，ADR-0014 将简报列为协同状态，因此两个写入口均返回 `409 FEATURE_DISABLED`。已有简报仍可列出，咨询种子仍可读取。 | `tests/workbench_api/test_briefing_api.py::test_disabled_scanner_keeps_existing_briefing_read_only`，红 2 → 绿 2，回读确认记录原样保留。 |

从本 worktree 根目录运行，`PYTHONPATH` 指向 `$PWD/core/src`。Python 与 pytest 均来自 `../../Talaria/.venv/bin`。

- `pytest tests/cli/test_cli.py tests/cli/test_graceful_shutdown.py tests/cli/test_service.py tests/workbench_api/test_runtime_features.py tests/workbench_api/test_service_config.py tests/workbench_api/test_briefing_api.py -q -p no:cacheprovider -m regression` → `20 passed, 52 deselected, 1 warning in 1.37s`。
- `python -m talaria contract-check --write` 后复跑 `python -m talaria contract-check` → 契约生成物零漂移。同步 `tests/workbench_api/openapi_before_router_split.json` 的三个 `409` 响应。`pytest tests/workbench_api/test_runtime_features.py tests/workbench_api/test_briefing_api.py tests/workbench_api/test_openapi.py -q -p no:cacheprovider` → `17 passed, 1 warning in 1.34s`。
- `pytest tests/perception tests/foundation tests/workbench_api tests/cli tests/evaluation tests/feed -q -p no:cacheprovider` → `6 failed, 607 passed, 1 skipped, 1 warning, 1 error in 22.06s`。`DSH_HOME` 指向不存在的测试目录，已安装 DSH Profile 的真实快照用例因此跳过。其余失败和错误均源于沙箱拒绝 TCP 绑定。
- 同一受影响套件以 `--deselect` 排除以下 7 项复跑 → `607 passed, 1 skipped, 7 deselected, 1 warning in 21.35s`。警告来自 Starlette 的 `httpx` 测试客户端弃用。

排除的回环 TCP 测试：

- `tests/workbench_api/test_collaboration_api.py::test_host_dispatch_ignores_ambient_http_proxy`
- `tests/cli/test_cli.py::test_up_dual_keeps_fast_path_when_dsh_fails`
- `tests/cli/test_cli.py::test_up_dual_keeps_fast_path_when_dsh_binary_missing`
- `tests/cli/test_dsh_host.py::test_host_starts_probes_and_stops_with_redacted_log`
- `tests/cli/test_graceful_shutdown.py::test_sigterm_shuts_down_gracefully`
- `tests/cli/test_usage_scan_service.py::test_usage_scan_fails_closed_when_service_endpoint_is_unreachable`
- `tests/cli/test_usage_scan_service.py::test_usage_scan_writes_through_running_service`

本单 8 项均已修复。真实 launchd 登录启动、崩溃重启和卸载需按上文人工步骤验证。本轮以 fake runner 验证 launchd 指令，没有访问真实数据目录。

**返工合并记录（2026-11-22）**

- 三个返工分支 `fix/data-access-01`、`fix/platform-01`、`fix/analysis-03` 依次并入 main（最后一个合并提交 `33f1bd4`）；`snapshot_builder.py` 冲突按“保留修订选版 + 行情关闭时不回填并报 FEATURE_DISABLED”合并。
- 合并后复跑：全量 `pytest` → 691 passed, 1 deselected；`talaria contract-check` 零漂移；desk-ui typecheck 通过，163 tests passed。
- 取舍：`up` 退出时改为无限期 `worker.join()` 后才释放数据目录锁。若写入线程卡死，进程会一直挂起、launchd 不会重启，需手动结束；换来的是不会出现两个写者。
