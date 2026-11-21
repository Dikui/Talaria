# 01 — launchd 常驻服务与功能开关

**What to build:** `talaria service install` 后，服务随登录启动、崩溃自动重启，关掉所有页面后 Notebook 仍能取数；`talaria service status` 显示运行状态与已开启的功能；行情、冷库写入、预警、扫描可以按配置单独开关。

**Blocked by:** None — can start immediately.

**Source of truth:** ADR-0014；`docs/architecture.md` §1.1、§8.1。

**Status:** ready-for-agent

- [ ] `install | uninstall | status` 三个子命令；plist 显式设置 `ProcessType` 与 `KeepAlive`，日志写入数据目录。
- [ ] 功能开关配置放在数据目录；关闭的功能不启动对应引擎，也不写对应存储。
- [ ] `talaria up` 前台模式保留。
- [ ] 服务启动时取得数据目录的排他锁（`runtime/service.lock`），同一数据目录第二个实例启动失败并说明原因。
- [ ] `talaria usage-scan` 不再另开写连接：服务运行时经服务接口提交，服务未运行时先取得同一把锁再写。
- [ ] 测试：配置解析与各开关组合下组合根装配的引擎；plist 生成内容快照；两个进程争用同一数据目录时第二个失败；服务运行时 `usage-scan` 的写入经服务落库。launchd 实际安装需在本机手动验证，在验收说明中记录步骤与结果。

## Comments

### 实现记录（platform-01）

- **launchd**：新增 `core/src/talaria/cli/service.py`。`render_launchd_plist` 是纯函数，plist 显式包含 `Label`、`ProgramArguments`、`RunAtLoad`、`KeepAlive`（`SuccessfulExit=false`）、`ProcessType=Interactive`、`ThrottleInterval=10`、`WorkingDirectory`、`StandardOutPath`、`StandardErrorPath`；日志默认写到 `<data-dir>/logs/service.{out,err}.log`。`install_service`/`uninstall_service`/`service_status` 的 `launchctl` 调用通过 `runner` 注入，测试用 fake runner，不执行真实 `launchctl`，也不写真实 `~/Library/LaunchAgents`。CLI 新增 `talaria service install|uninstall|status`，集中在 `_cmd_service`；`service status` 不启动服务。
- **功能开关**：新增 `<data-dir>/service.json`（`service_config.py`），四项 `market_data`/`cold_store`/`alerts`/`scanner`，缺省全开；`install` 首次写入全开配置且不覆盖已有文件；未知键、非 bool、坏 JSON、版本不符均报出配置路径。`AppSettings.features` 传入组合根 `build_runtime`：关闭行情采集不建实时流/`FeedSupervisor`/热缓冲/订阅调度；关闭冷库写入不建 `ColdStore` 与 `ColdPublisher`；关闭预警不建 `AlertService`/ticker/listener；关闭扫描不建 `ScannerService`/ticker/listener。关闭功能的 HTTP 写入口返回 `409 FEATURE_DISABLED`（`Runtime.require_alerts/require_scanner`），不启动对应引擎、不写对应存储。
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
