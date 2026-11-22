# 03 — 常驻服务托管 DSH 宿主，`talaria open` 打开工作台

**What to build:** `talaria service install` 之后，登录即可用 DSH 工作台：常驻服务以子进程方式启动 DSH 宿主，DSH 崩溃后自动重启，服务本身不受影响。交易员执行 `talaria open`，浏览器直接打开带 token 的 DSH 入口；DSH 不可用时打开服务托管的兜底页面并说明原因。

**Blocked by:** None — can start immediately（与 `platform/02` 改的文件不重叠，可并行）。

**Source of truth:** ADR-0020（后果第一条）、ADR-0014 常驻服务部分；`docs/architecture.md` §8.1。

**Status:** ready-for-agent

## 现状

- `platform/01` 的 launchd Agent 固定跑 `talaria up --solo`（`core/src/talaria/cli/service.py` 的 `install_service`），服务不托管 DSH。
- 前台 `talaria up`（双进程）已经用 `cli/dsh_host.py` 的 `DshHost` 启停 DSH：读 stdout 拿带 token 的入口 URL，崩溃只标记 `offline`，不重启。入口 token 只留在内存里，API 也不暴露它。
- launchd 启动的进程 `PATH` 通常不含 `/opt/homebrew/bin`，按名字找 `dsh` 会失败。

## 验收

- [ ] `service install` 默认安装双进程模式（不再带 `--solo`），`--solo` 作为可选参数保留。安装时解析 `dsh` 的绝对路径（`--dsh-bin` / `TALARIA_DSH_BIN` / `PATH`）写进 plist 参数；找不到时安装失败并提示，或在用户给了 `--solo` 时照常安装。plist 显式写入 `DSH_HOME`（取安装时的值，缺省 `~/.dsh`）。
- [ ] DSH 崩溃后由 `DshHost` 自动重启，指数退避（如 1s 起、上限 60s），连续失败期间状态为 `offline` 并记录最近一次失败原因；重启成功后恢复 `online`，桥接描述文件照常可用。服务退出时先停 DSH。快通道在整个过程中不受影响（现有 `test_up_dual_keeps_fast_path_*` 继续通过）。
- [ ] DSH 每次上线后，把入口 URL（含 token）写入 `<data-dir>/runtime/dsh-entry.json`，权限 0600，目录 0700；DSH 下线或服务退出时删除。日志和 API 里的 URL 仍按现有规则打码。
- [ ] `talaria open [--data-dir]`：
  - 服务在运行且 DSH `online` → 用系统默认浏览器打开 `dsh-entry.json` 中的 URL。
  - 服务在运行但 DSH 不在线 → 打开兜底页面 `<base_url>/desk`，在终端说明 DSH 状态与日志路径。
  - 服务没运行 → 不启动服务，提示用 `talaria service install` 或 `talaria up`，退出码非 0。
  - `--print` 只打印将要打开的地址（token 打码）不打开浏览器，供脚本和测试使用。
- [ ] `service status` 增加一行 DSH 宿主状态（`online` / `starting` / `offline` / 未启用）、pid 与日志路径。
- [ ] 文档：`README.md` 的启动说明和 `docs/architecture.md` §8.1 运行方式改为“`service install` 后用 `talaria open`”，`--solo` 写成可选。
- [ ] 测试：plist 快照（默认双进程、`--solo`、`dsh` 绝对路径与 `DSH_HOME`）；用 `tests/fixtures/m2/fake_dsh_host.py` 模拟 DSH 崩溃后重启与退避；`dsh-entry.json` 的权限、写入与删除时机；`open` 三种情况下的目标地址（用 `--print`）。真实 launchd 安装、登录自启与 `open` 打开浏览器在本机手动验证，在验收说明中记录步骤与结果。

## Comments

### 实现记录（platform-03）

- `service install` 默认安装双进程，支持 `--solo`。DSH 可执行文件按 `--dsh-bin`、`TALARIA_DSH_BIN`、`PATH` 的顺序查找，绝对路径写入 plist。plist 保存安装时的 `DSH_HOME` 和双进程的 `PATH`，本机 npm 版 DSH 用 `/usr/bin/env node` 启动。
- 服务在后台启动和监督 DSH。首次启动失败或宿主退出后按 1 秒起步、最高 60 秒的指数退避重试，稳定在线 60 秒后重置退避。退避期间报告最近一次失败，恢复时重新发布入口。服务收到退出信号后先停 DSH，再关闭服务和释放数据目录锁。桥接描述文件在 DSH 的各次重启之间保持可用。
- 入口文件原子写入 `runtime/dsh-entry.json`（0600，目录 0700）。DSH 离线和正常退出时清理。异常强退可能留有旧文件，下一次服务启动会清理，`open` 也会检查服务锁和进程 PID。日志与 `open --print` 遮盖 token，原 API 继续只发布无查询串的 DSH 基址。
- `open` 按 `--data-dir`、`TALARIA_DATA_DIR`、默认作业 plist、`./.talaria` 查找服务。服务在线时，通过协同接口获取宿主状态，对照服务与 DSH 的 PID 后读取入口。入口不可用时打开 `/desk`，终端显示宿主状态和日志位置。未找到可用服务描述时退出 2。README 与架构 §8.1 已更新。本单与指定 ADR 无冲突。

### 自动验证

从本 worktree 使用 `PYTHONPATH=$PWD/core/src /Users/dikui/Project/Talaria/.venv/bin/python -m pytest`，各次运行均加 `-q -p no:cacheprovider`。

| 测试范围及筛选 | 通过 | 失败 |
|---|---:|---:|
| `tests/cli/test_service.py -k 'launchd_plist_renders or install_writes'`，红 → 绿 | 0 → 6 | 6 → 0 |
| `tests/cli/test_dsh_host.py -k restarts`，红 | 0 | 1 |
| `tests/cli/test_dsh_host.py -k 'not starts_probes'`，绿 | 5 | 0 |
| `tests/cli/test_open.py`，红 → 绿 | 0 → 8 | 8 → 0 |
| `tests/cli/test_service.py -k install_writes`，补测 plist 的 `PATH`，红 | 2 | 3 |
| `tests/cli/test_service.py tests/cli/test_open.py tests/cli/test_dsh_host.py tests/cli/test_cli.py tests/cli/test_graceful_shutdown.py` | 52 | 4 |
| 同上一行，加 `-k 'not starts_probes and not keeps_fast_path and not sigterm_shuts_down_gracefully'` | 52 | 0 |
| `tests/workbench_api/test_app.py -k collaboration` | 3 | 0 |

以下 4 项在回环 TCP 绑定时被沙箱拒绝（`PermissionError: [Errno 1] Operation not permitted`），需在本机复跑。

- `tests/cli/test_dsh_host.py::test_host_starts_probes_and_stops_with_redacted_log`
- `tests/cli/test_cli.py::test_up_dual_keeps_fast_path_when_dsh_fails`
- `tests/cli/test_cli.py::test_up_dual_keeps_fast_path_when_dsh_binary_missing`
- `tests/cli/test_graceful_shutdown.py::test_sigterm_shuts_down_gracefully`

### 本机验收步骤

真实 launchd 安装、登录自启和系统浏览器打开均未执行。完成 README 中的构建及插件安装后，在 DSH 端口 8766 空闲的 macOS 会话中执行以下步骤。

1. 用独立标签安装：`uv run talaria service install --label com.talaria.platform03 --data-dir "$HOME/.talaria-platform03" --port 18765`。运行 `uv run talaria service status --label com.talaria.platform03`，确认宿主变成 `online` 且显示 PID 和日志。退出登录后重新登录，确认相同命令仍显示服务和宿主在线。
2. 运行 `uv run talaria open --data-dir "$HOME/.talaria-platform03"`，确认默认浏览器打开可用的 DSH 工作台。加 `--print` 时只输出脱敏地址。用 `stat -f '%Lp' "$HOME/.talaria-platform03/runtime/dsh-entry.json"` 确认权限为 600。
3. 结束 `service status` 显示的 DSH 进程，确认服务 PID 不变，`http://127.0.0.1:18765/desk` 持续可用。`open --print` 在宿主离线时打印 `/desk` 地址，宿主恢复后打印新的脱敏 DSH 入口。
4. 运行 `uv run talaria service uninstall --label com.talaria.platform03`，确认入口文件已移除且 `open --data-dir "$HOME/.talaria-platform03"` 退出 2 并提示启动命令。
