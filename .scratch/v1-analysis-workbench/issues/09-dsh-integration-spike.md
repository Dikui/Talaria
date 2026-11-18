# 09 — DeepSeek Harness 接入预研：`talaria up` 双进程、同屏嵌入看盘组件与第一个只读工具

**What to build:** 尽早验证 M2 的前提是否成立：执行 `talaria up` 同时启动 Python 守护进程与 `talaria-desk` 协同宿主，看盘组件包嵌入 DSH 同屏插槽并正常显示 K 线，智能体能通过受控桥调用 `get_snapshot` 读到冻结快照。若 DSH 0.x 接口不支持嵌入或桥接，在本任务暴露并回改架构规范 / ADR，而不是等到 M1 做完。

**Blocked by:** 01 — 核实并锁定外部依赖版本与许可证（第 1 项 DSH 结论）；02 — 最窄贯通：Bybit 历史 K 线回填、证据快照冻结与 `--solo` 看盘页显示 K 线

**Source of truth:** [需求文档](../spec.md)、[领域词汇表](../../../GLOSSARY.md)、[系统分层架构规范](../../../docs/architecture.md)、[ADR-0001](../../../docs/adr/0001-dual-plane-dsh-and-python-core.md)、[ADR-0005](../../../docs/adr/0005-fast-path-bypass-and-decoupled-chart-workbench.md)

**Status:** ready-for-agent

- [x] `talaria up` 纳管双进程健康探针、日志分流与优雅退出；`--solo` 行为不变。
- [x] 看盘组件包同一份代码既可由快通道独立打开，也可嵌入 DSH 插槽（不引入 SolidJS 外围封装）。
- [x] 受控桥把 `get_snapshot` 封装为符合 DSH Schema 校验子集的工具，智能体调用后返回绑定 `snapshot_id` 的结果。
- [x] `talaria-desk` Profile 在配置补丁层移除任何下单或绕过人工确认的执行工具，挂载 DSH 原生单调执行守卫（拦截从历史轨迹 `fork` 发起的重放写操作）；`dsh --dump-config` 快照测试固化权限边界。
- [ ] DSH 宿主停止或卡住时，浏览器经快通道的切图与画线不受影响（以手动或脚本验证并记录）。
- [x] 离线测试用模拟 / 录制的桥接消息验证工具契约；真实模型会话列为沙箱外可选冒烟验证。
- [x] 结论（可行 / 需调整的接口与位置）追加到本任务单 `## Comments`。
- [x] 浏览器界面验收（均附截图）：宽度 `1440` 与 `390`、浅色与深色、简体中文下检查同屏布局（图表区 + 协同面板）与宿主离线时的非阻塞提示。

## Comments

### 2026-11-19 — 实现结论（预研）

**结论：可行（`dsh 0.2.2-rc.3` / SDK `0.2.2rc2`）。** `talaria up` 已能同时拉起 Python 守护进程与 `talaria-desk` DSH 宿主，看盘组件以 DSH 原生右侧栏页签嵌入，`get_snapshot` 经受控桥注册为只读工具，Profile 权限边界与原生单调守卫均已挂载。以下接口限制需要后续固化，不能按“DSH 0.x 已稳定”理解。

#### 1. `talaria up` 双进程

- `DshHost` 启动命令为 `dsh --profile talaria-desk --no-open --port <dsh_port>`；不再传 `--patch`：插件包的 `dsh.bundle.patch` 已自动叠加，重复传会产生重复 `preset-talaria-desk` / `talaria-desk-plugin` 行（首版已踩坑，已加 dump 单测防回归）。
- 宿主 stdout 读到入口 URL 即视为可探测；健康探针接受 HTTP < 500（`/` 无 token 返回 401 也算在监听）。stdout 合并进 `$TALARIA_DATA_DIR/logs/dsh-host-*.log`，日志中的 `?token=` 被替换为 `***`；终端仍打印带 token 的入口 URL。
- `SIGINT/SIGTERM` 先停 uvicorn，再对整个 DSH 进程组 `SIGTERM`，超时 `SIGKILL`；`--solo` 路径未改。
- **关键行为（按 lead 反馈改）**：DSH 缺失或启动失败时**不再**让 `talaria up` 退出：守护进程照常监听，`/api/collaboration` 返回 `host: offline` 与中文 `detail`，`/desk`、`/api/health`、`/api/snapshots` 全部可用；终端打印修复提示与 `--solo` 提示。测试 `tests/cli/test_cli.py::test_up_dual_keeps_fast_path_when_dsh_fails` 用一个退出码 7 的假 `dsh` 断言了健康、快照与 `/desk`；`test_up_dual_keeps_fast_path_when_dsh_binary_missing` 断言二进制缺失时 `/api/collaboration` 为 `host=offline`（不是一直 `starting`）、`/api/health` 与 `/desk` 仍 200。
- `talaria dsh-install` 提供可重复安装：构建 `@talaria/dsh-plugin` → 初始化 `~/.dsh/profiles/talaria-desk` → `dsh plugin --profile talaria-desk add file:<repo>/dsh/packages/talaria-dsh-plugin`。插件 `client/index.js` 是构建产物，不提交；`dsh-install` 会先构建。

#### 2. 同屏嵌入（无 SolidJS）

- 同一份 `DeskWorkbench` 组件：独立入口 `src/main.tsx` 渲染 `<DeskWorkbench />`，DSH 插件渲染 `<DeskWorkbench embedded />`；没有复制 React 应用，也没有 SolidJS 外围壳。
- 客户端插件使用 rc.3 自带 API：`ctx.sidebarRightTabs.register({ id, kind, title, keepMounted, canOpen, guide })` + `ctx.slots.register({ name: 'sidebar.right.pane.tab', key: 'talaria-desk-tab' }, Body)`，并在有会话后 `ctx.sidebarRight.openTab('talaria-desk')` 自动打开一次。
- 客户端模块按 DSH 协议包成 `window.__ModuleLoader__.load({ id, factory(require) {...} })`，React 由宿主模块表提供；klinecharts 依赖 `process.env.NODE_ENV`，构建脚本在 factory 内补 `process` 兜底。
- 宿主与看盘页不同源：宿主插件用 `ctx.webServer.register({ kind: 'exact', path: '/talaria/bootstrap', handler })` 下发 Python 回环基址 + Bearer Token；Python 端增加仅回环来源的 CORS 白名单，`fetch` 与 `/ws/desk` 都直连守护进程，不经过 DSH。
- 主题只在 `.talaria-desk` 包装层作用域内切换，宿主主题不被改动。

#### 3. 受控桥 `get_snapshot`

- `@talaria/dsh-plugin` 的 `index.js` 读取 `get_snapshot.tool.json`，以 `parameters = inputSchema`、必填 `output.schema` 注册工具；`execute` 经 `bridge.js` 读取 `$TALARIA_DATA_DIR/runtime/dsh-bridge.json`（0600）或 `TALARIA_DSH_BRIDGE`，只允许回环 HTTP，POST `/api/snapshots` 携带 Bearer，返回 `SnapshotMeta`；`renderSnapshot` 强制带 `snapshot_id`。
- **DSH Schema 子集实测比文档更窄**：只接受 `type / oneOf / properties / required / additionalProperties / items / enum / const` 与 `description / title / default / examples`；`anyOf`、`minItems`、`minLength`、`minimum` 一律拒绝，对象型 `additionalProperties` 必须是布尔。首版宿主启动直接报 `JsonSchemaError: unsupported JSON schema ...`。已在 `core/src/talaria/foundation/contracts_io.py` 增加 `_to_dsh_subset`（`anyOf→oneOf`、递归裁剪、对象 `additionalProperties→true`）并让 `contract-check` 固化；根级 `$schema`/`$defs` 仍为零。
- 真实模型调用未在沙箱内跑：工具注册、Schema 校验、`/talaria/bootstrap` 与桥接 HTTP 均在真实 rc.3 宿主加载（无 `entry did not activate`），工具执行契约由 node 离线测试覆盖。

#### 4. `talaria-desk` 权限边界与单调守卫

- `dsh/profiles/talaria-desk.patch.yml`：`sandbox-policy.mode: read-only`，`approval.policy: ask`，`permission.presets` 只保留 `read-only`（整表替换，`danger-full-access` 不再存在）；默认 agent preset 切到 `talaria-desk`，只挂 `persona / time-context / tool-fs(read, write:false, edit:false, applyPatch:false) / tool-fs-search / tool-todo / tool-web / tool-ask-user / compaction`，不挂 bash、terminal、write/edit/apply_patch。
- 原生单调守卫：`ctx.tools.guard(createDeskGuard(...))`。守卫先按只读白名单（`get_snapshot/read/glob/grep/web_search/web_fetch/todo_write/ask_user_question`）拒绝所有其他工具；对 `session.header.isSeeded` 的会话，再扫描 `seq < session.inheritedEventCount` 的 `tool/call`，用 `name + stable-stringified arguments` 指纹匹配，命中即拒绝。守卫只有 denial，没有 allow，符合 DSH 的单调语义。
- **验证方式**：`dsh --profile talaria-desk --dump-config` 输出锁进 `tests/fixtures/dsh/talaria-desk.dump.snapshot.txt`，`tests/cli/test_talaria_desk_profile.py` 断言权限块、单例 preset/plugin、无 `danger-full-access`；守卫行为由 `dsh/packages/talaria-dsh-plugin/test/guard.test.mjs` 用 fake `Session` 覆盖（允许只读、拒绝 fork 重放、拒绝改写参数、缺 session 时 fail-closed）。守卫不出现在 dump 中，因此不把 dump 当守卫证明。
- **未做**：真实模型会话里从历史轨迹 fork 后触发写工具重放的端到端验证（需要真实模型/沙箱外环境）；当前只有 node 行为测试 + 真实宿主编译激活证据。

#### 5. 宿主停止 / 卡住

- 实测 `SIGTERM` 杀 DSH 后：`/api/collaboration` 立即变 `offline`（`DSH 宿主意外退出（退出码 0）`），`/desk` 仍 200，独立页在 1m↔5m 切图并重新取 bars，快照从 `b40c4612` 换到 `a4ccc816`；界面提示“协同宿主：离线 · 快通道正常”。
- **画线未验证**：当前 `talaria-desk-ui` 还没有画线/叠加编辑 UI（本单未实现），所以“切图与画线不受影响”只能确认切图、刷新与指标重算；画线留待有画线组件后复测。任务单第 5 项因此保持未勾选。

#### 6. 测试与构建

- `uv run pytest -q` → **251 passed, 1 deselected**（含新增 `test_dsh_host.py`、`test_cli.py::test_up_dual_keeps_fast_path_when_dsh_fails`、`test_talaria_desk_profile.py`、`test_dsh_tool_schema.py`）。
- `pnpm -C dsh build` → 3 个包全绿；`pnpm -C dsh test` → desk-ui 42 + plugin 12 = **54 passed**；`uv run talaria contract-check` → **契约生成物零漂移。**
- 真实 Profile：`dsh --profile talaria-desk --dump-config` 退出 0，`preset-talaria-desk`/`talaria-desk-plugin` 各 1 行；实际 `talaria up` 无激活 warning。
- 复现命令：`uv run talaria dsh-install`（首次）→ `uv run talaria up --port 8790 --dsh-port 8791 --data-dir /tmp/talaria-up-test2 --fixture-dir tests/fixtures/bybit`。

#### 7. 浏览器验收（ego-browser，非 Playwright）

均为简体中文界面，`read_image` 已确认非空白：

| 场景 | 尺寸 | 截图 |
|---|---|---|
| 宿主 + 右侧看盘面板（深色） | 1440×900 | `.scratch/tasks/ticket-09/dsh-1440-dark.png` |
| 宿主 + 右侧看盘面板（浅色） | 1440×900 | `.scratch/tasks/ticket-09/dsh-1440-light.png` |
| 右侧面板全屏（深色） | 390×844 | `.scratch/tasks/ticket-09/dsh-390-dark.png` |
| 右侧面板全屏（浅色） | 390×844 | `.scratch/tasks/ticket-09/dsh-390-light.png` |
| 宿主被杀后独立 `/desk`（深色） | 1440×900 | `.scratch/tasks/ticket-09/desk-offline-1440-dark.png` |

断言：页签 `Talaria 看盘` 可见且自动打开一次；`协同宿主：在线`；`Access mode, current: Read Only`；K 线 + EMA/RSI/ATR 渲染；宿主机停止后独立页徽标为 `协同宿主：离线 · 快通道正常` 且切图可用。注：ego 的视口只能在单次 `ego_script` 内用 `Emulation.setDeviceMetricsOverride` 生效，截图 PNG 尺寸与 CSS 视口一致。

#### 8. 已回改的架构 / ADR 位置

- `docs/architecture.md` §5.1：DSH 启动命令改为 `dsh --profile talaria-desk --no-open --port <dsh_port>`；补 `runtime/dsh-bridge.json`（0600）与“宿主失败不阻塞快通道”；版本基线删除独立 `ui-slots 0.0.1-rc.1` 依赖，改为 rc.3 自带客户端插槽；dump 命令改为 `dsh --profile <name> --dump-config`。
- `docs/architecture.md` §5.3 ③：明确 dump 只做权限/插件快照，守卫必须行为测试；09 只覆盖 `talaria-desk`，`talaria-auto/eval` 留 M2。
- `docs/research/external-dependency-verification-v1.md`：更新回改状态并追加“09 实测补充”（客户端插槽、`dsh.bundle.patch` 重复、Schema 子集、`webServer.register`、guard 不可 dump）。
- **ADR 无需改结论**：ADR-0001 的双平面/DSH 作为 Plane A、ADR-0005 的快通道绕开 DSH 与零 DSH 依赖组件包，均被本次实测支持；仅校准 ADR-0001 中“独立 `ui-slots 0.0.1-rc.1` 为基线”的事实（09 实测 rc.3 自带插槽 API，不再单独使用该包），不改变 ADR 结论。ADR-0005 的 `10.1.1` 版本与 `calc` 结论保持成立。
