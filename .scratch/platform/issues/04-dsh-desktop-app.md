# 04 — Talaria 装进 DSH 桌面应用，服务不再托管 DSH

**What to build:** 交易员打开 DSH 桌面应用，侧边栏里就有图表、预警、扫描、关键位四个入口，点开即是 platform/02 做好的面板；会诊、简报和分析工具照常可用。常驻服务只跑数据并写出桥接凭据；`talaria open` 打开桌面应用。装进 `desktop` profile 后，交易员在桌面应用里的其他会话行为不变。

**Blocked by:** None — can start immediately.

**Source of truth:** ADR-0021（修订 ADR-0020 后果第一条）、ADR-0020、ADR-0005；`docs/architecture.md` §1.1、§8.1。

**Status:** ready-for-agent

## 现状

- 桌面应用：`/Applications/DeepSeek Harness.app`，版本 0.2.2-rc.5，profile 目录 `~/.dsh/profiles/desktop`（已装有交易员的第三方插件，如 `dsh-better-sidebar`、`dsh-context`）。自带命令行：`/Applications/DeepSeek Harness.app/Contents/Resources/runtime/cli/bin/dsh`。全局 `dsh` 命令已不存在。
- `talaria dsh-install` 默认装到 `talaria-desk` profile，并要求 PATH 里有 `dsh`。
- 插件 `dsh/packages/talaria-dsh-plugin` 的 `package.json` 声明了 `dsh.bundle.patch`（`talaria-desk.patch.yml`：只读沙箱、审批、默认预设改为 talaria-desk）；`index.js` 用 `ctx.tools.guard` 全局注册只读守卫。装进 `desktop` profile 会改掉交易员所有会话。
- 插件经 `bridge.js` 读取凭据：`TALARIA_DSH_BRIDGE` 环境变量、`cwd/.talaria/runtime/dsh-bridge.json`、`~/.talaria/runtime/dsh-bridge.json`。桥接文件只在服务托管 DSH 时写出。
- platform/03 让 launchd 服务托管 DSH 子进程（`cli/dsh_host.py`、`cli/service.py`）。

## 验收

- [ ] 安装：`talaria dsh-install` 默认装到 `desktop` profile（`--dsh-profile` 可改）；`dsh` 按 `--dsh-bin`、`TALARIA_DSH_BIN`、PATH、桌面应用自带命令行的顺序查找。安装时把凭据文件的绝对路径（`<data-dir>/runtime/dsh-bridge.json`，数据目录按 `talaria open` 的解析顺序）写进插件配置。重复执行幂等。
- [ ] 不改交易员的 profile 设置：装进 `desktop` profile 后，profile 的默认预设、沙箱与审批设置不变（`dsh --profile desktop --dump-config` 前后对比，只多出 Talaria 插件与一个非默认的“Talaria 分析副驾驶”预设）。限制性补丁只在 `talaria-desk` profile 生效。
- [ ] 守卫只作用于 Talaria 会话：会诊（`talaria-consult-` 前缀）、简报（`talaria-briefing-` 前缀）以及选用 Talaria 预设的会话受只读守卫约束；其他会话的工具调用不受影响。补测试覆盖两种会话。
- [ ] 凭据：常驻服务无论是否托管 DSH 都写出 `dsh-bridge.json`（0600），退出时删除；插件每次调用前检查文件是否变化（mtime 或内容），服务重启换 token 后无需重启桌面应用即可恢复；凭据缺失或服务未运行时，面板显示清楚的离线说明，不抛未处理异常。
- [ ] 服务：`talaria service install` 不再托管 DSH（去掉 `--dsh-bin`、`DSH_HOME`、`PATH` 等 DSH 相关 plist 参数，`--solo` 成为唯一行为后移除该参数或保留为无操作的兼容参数）；`service status` 的 DSH 一行改为显示桌面应用是否安装、是否运行，以及 Talaria 插件是否在其 profile 中。`talaria up` 的双进程模式保留给开发与测试。
- [ ] `talaria open`：服务在运行时打开桌面应用（`open -a "DeepSeek Harness"`）；桌面应用未安装时打开兜底页面 `<base_url>/desk` 并说明；服务未运行时提示启动命令，退出码非 0。`--print` 打印将要执行的动作。
- [ ] 侧边栏：在 rc.5 桌面应用中，四个入口默认出现在侧边栏（不在 More 折叠里），与已装的 `dsh-better-sidebar` 共存。以 rc.5 自带的客户端包源码与类型为准（`/Applications/DeepSeek Harness.app/Contents/Resources/` 下查找 `dsh-client-ui-layout`、`dsh-client-ui-sidebar`），不要猜接口。
- [ ] 文档：README 启动说明、`docs/architecture.md` §1.1 与 §8.1、ADR-0020 后果第一条加“已由 ADR-0021 修订”的标注；DSH 版本基线改为 0.2.2-rc.5。
- [ ] 测试：插件测试覆盖守卫作用范围、凭据重读、安装后的插件配置；CLI 测试覆盖 `dsh-install` 的查找顺序与配置写入（不执行真实 `dsh`）、`service install` 的 plist 不含 DSH 参数、`open` 三种情况。真实桌面应用侧边栏与 profile 设置对比由人工在本机验证，步骤写进实现记录。

## Comments

### 实现记录（platform-04）

- `dsh-install` 默认选择 `desktop`，按显式参数、环境变量、PATH、桌面自带 CLI 查找。已经显式指定的可执行文件若无效，直接报告路径错误。插件打成目标 profile 专用的本地包，绝对凭据路径用 `open` 的规则解析，构建相同内容时复用同一个安装包。rc.5 要求桌面应用先初始化 `desktop`。
- 默认插件包只插入插件与可选的「Talaria 分析副驾驶」预设。安装到 `talaria-desk` 才附加默认预设、沙箱和审批的限制性补丁。以 rc.5 的 Session 预设投影识别 Talaria 会话，保留会诊、简报与 fork 写入重放限制。普通会话的工具调用和右栏默认行为由各自的配置决定。
- 常驻服务就绪后原子发布 0600 的 `dsh-bridge.json`，退出时清理。工具及认证后的 bootstrap 每次请求重读凭据。打开的面板定期刷新凭据并直连服务健康接口，离线时提示如何启动。桌面插件发布带 PID 的本机协同地址，常驻服务据此派发会诊与简报。
- launchd 固定运行 `up --solo`，旧的 `service install --solo` 保留为兼容参数。`service status` 报告桌面应用安装、运行和插件状态。`open --print` 输出启动动作。`up` 的开发双进程模式继续通过 stderr 提供 Web 宿主入口。
- rc.5 的 `dsh-client-ui-layout` 提供主区 keyed slot，`dsh-client-ui-sidebar` 的 `sidebar.panellist` 注册默认直接显示。桌面来源 `dsh-app://app` 通过常驻服务的 CORS 白名单与联动 iframe 的父窗口校验。rc.5 配置合成器对官方 Web bundle 与 `dsh-better-sidebar` 做静态合成验证，桌面层仅新增插件与可选预设，开发层仍应用原限制性配置。
- 任务单与 ADR-0021 一致。原架构 §1.1、§8.1 及 ADR-0020 后果第一条仍描述常驻服务托管 DSH，按 ADR-0021 更新。桌面应用与系统浏览器间的跨应用联动需实测。静态配置合成、注入的启动命令与预检测试覆盖自动化边界。

#### 自动验证

Python 命令从本 worktree 运行，前缀为 `PYTHONPATH=$PWD/core/src /Users/dikui/Project/Talaria/.venv/bin/python -m pytest`，统一使用 `-q -p no:cacheprovider`。

| 范围及筛选 | 通过 | 失败 |
|---|---:|---:|
| `tests/cli/test_dsh_install.py`，首次使用未安装的 YAML 测试依赖 | 0 | 1 项收集错误 |
| `tests/cli/test_dsh_install.py --tb=no`，补齐无新增依赖的测试，红 | 0 | 12 |
| `tests/cli/test_dsh_install.py`，安装包路径断言纠正前 → 后 | 0 → 12 | 12 → 0 |
| `tests/cli/test_open.py tests/cli/test_service.py -k 'open or install_writes or desktop_and_profile' --tb=no`，红 | 4 | 8 |
| `tests/cli/test_open.py tests/cli/test_service.py`，绿 | 27 | 0 |
| `tests/cli/test_cli.py tests/workbench_api/test_app.py -k 'publishes_private_bridge or finds_desktop_collaboration or dsh_desktop_origin' --tb=short`，红 → 绿 | 0 → 3 | 3 → 0 |
| `tests/cli/test_{dsh_install,open,service,dsh_host,cli,graceful_shutdown,talaria_desk_profile}.py tests/workbench_api/test_{app,collaboration_api,briefing_api}.py tests/foundation/test_import_boundaries.py --tb=short` | 172 | 6 |
| 同一组路径，加 `-k 'not starts_probes and not keeps_fast_path and not sigterm_shuts_down_gracefully and not real_dump_config and not ignores_ambient_http_proxy'` | 172 | 0（6 项筛除） |

- Node 插件定向测试通过 `node --test --test-name-pattern <模式> dsh/packages/talaria-dsh-plugin/test/index.test.mjs` 运行。单独的 `only Talaria`、`bootstrap reloads` 和 `desktop plugin publishes` 每项先失败 1 项。随后 `only Talaria` 单项 1 通过，`bootstrap reloads|only Talaria` 2 通过，`desktop plugin publishes|only Talaria|bootstrap reloads|rejects duplicate` 4 通过，最终 `desktop plugin publishes` 1 通过。
- `pnpm -C dsh --filter @talaria/dsh-plugin test`，24 通过、9 失败。用 `node --test --test-name-pattern 'missing credential|non-loopback|tool output schema|allowlisted|fork|changed arguments|non-seeded|missing session|inherited fingerprints|controlled writes|guard|malformed|bootstrap|desktop plugin|root panels|rolls back|returning to|docking|consultation charts' dsh/packages/talaria-dsh-plugin/test/*.test.mjs` 筛出可在沙箱运行的用例，24 通过。
- `pnpm -C dsh --filter @talaria/desk-ui test tests/workbench/link.test.ts -t 'rc.5 desktop'`，红 1 失败，绿 1 通过。`pnpm -C dsh --filter @talaria/desk-ui test`，194 通过。`pnpm -C dsh typecheck` 与 `pnpm -C dsh build` 均通过。默认离线安装因沙箱无法硬链用户包缓存而缺少 tarball。将缓存复制到临时目录后，用 `pnpm -C dsh install --offline --frozen-lockfile --store-dir <临时缓存> --package-import-method=copy` 安装成功。
- `PYTHONPATH=$PWD/core/src /Users/dikui/Project/Talaria/.venv/bin/python -c "import sys; from talaria.cli.main import main; sys.exit(main(sys.argv[1:]))" contract-check` 通过，零漂移。
- `grep -rnE -- '--dsh-bin|DSH_HOME|(^|[^a-zA-Z_])PATH([^a-zA-Z_]|$)|DshHost|dsh-entry\.json' core/src/talaria/cli/service.py` 与 `grep -rnE -- 'sandbox-policy|dsh-user-approval|dsh-permission-presets|default: talaria-desk' dsh/packages/talaria-dsh-plugin/cordis.patch.yml` 均退出 1，零匹配。

沙箱拒绝回环 TCP 绑定（EPERM），影响以下 Python 用例。

- `tests/cli/test_dsh_host.py::test_host_starts_probes_and_stops_with_redacted_log`
- `tests/cli/test_cli.py::test_up_dual_keeps_fast_path_when_dsh_fails`
- `tests/cli/test_cli.py::test_up_dual_keeps_fast_path_when_dsh_binary_missing`
- `tests/cli/test_graceful_shutdown.py::test_sigterm_shuts_down_gracefully`
- `tests/workbench_api/test_collaboration_api.py::test_host_dispatch_ignores_ambient_http_proxy`

DSH 插件同样有 9 项被拒绝绑定，包括 `test/bridge.test.mjs` 中 7 项本机 HTTP 用例和 `test/index.test.mjs` 中 2 项会诊、简报 HTTP 用例。另有 `tests/cli/test_talaria_desk_profile.py::test_real_dump_config_matches_permission_snapshot` 因沙箱禁止写入本机开发 profile 的 `cordis.yml` 而失败。真实桌面 profile 安装及验收留给本机。

#### 本机验收

1. 用 rc.5 桌面应用初始化 `desktop` 后完全退出。设置 `DSH_BIN='/Applications/DeepSeek Harness.app/Contents/Resources/runtime/cli/bin/dsh'`，执行 `"$DSH_BIN" --profile desktop --dump-config > /tmp/talaria-desktop-before.yml`。运行 `uv run talaria dsh-install --dsh-bin "$DSH_BIN"` 两次，再执行同一 dump 命令写到 `/tmp/talaria-desktop-after.yml`。用 `diff -u` 核对有效配置只多插件和可选预设，并检查默认预设、沙箱和审批值。
2. 运行 `uv run talaria service install`、`uv run talaria open --print` 和 `uv run talaria open`。在应用侧栏直接打开图表、预警、扫描和关键位，检查 More 初始折叠时仍可见，并使用 `dsh-better-sidebar` 的入口。通过 DevTools 检查看盘 HTTP 和 WS 直连常驻服务，并检查同组面板联动。
3. 用普通会话执行正常工具调用，再选用「Talaria 分析副驾驶」验证同一写工具被守卫拦截。触发一轮会诊和一轮简报，核对结果、分析工具和会诊图表。
4. 桌面保持打开，停止常驻服务并检查离线提示、桥接文件删除。重新启动服务，检查文件权限 `stat -f '%Lp' <data-dir>/runtime/dsh-bridge.json` 为 600，面板和写操作恢复。用 `talaria service status` 核对退出桌面应用前后的运行状态。

#### 提交状态

`git diff --check` 通过。`git diff main...HEAD` 无已提交的任务差异，实际改动按工作区相对 HEAD 复核。逐路径暂存时，沙箱拒绝在主仓库的 worktree 元数据目录创建 `index.lock`。改动保留在 `ticket/platform-04` 工作区，任务单状态仍为 `ready-for-agent`。
