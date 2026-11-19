# 20 — M2 收尾：双平面协同工作台端到端验收

**What to build:** 以交易员视角走通 M2 全流程并修复发现的缺陷：`talaria up` → 框选会诊得到 `Setup Insight` → 智能体写标注（含被门控拦截的一次）→ 采纳标注 → 确认智能体预警草案 → 收到扫描简报并带入对话 → 停掉宿主后快通道照常可用。

**Blocked by:** 16 — M1 收尾；18 — 智能体图表标注硬门控与智能体预警草案；19 — 异动扫描协同简报

**Source of truth:** [需求文档](../spec.md)（用户故事 8–10）、[系统分层架构规范](../../../docs/architecture.md)（§1.3 M2、§5.3 契约门禁）

**Status:** ready-for-agent

- [x] 一条场景回归用例用录制的桥接消息走通上述全流程。
- [x] 架构规范 §5.3 中的 DSH 工具 Schema 子集门禁与 `talaria-desk` Profile `dsh --dump-config` 快照门禁通过。
- [ ] 真实模型会话冒烟（沙箱外可选）跑通一次并记录结果。
- [x] 浏览器界面验收（均附截图）：宽度 `1440` 与 `390`、浅色与深色、简体中文下检查完整协同工作台；覆盖宿主离线与数据源断流同时发生时的页面。

## Comments

### 场景回归

新增 `tests/acceptance/test_m2_copilot.py`：真实 `talaria up` 双子进程（看盘守护 + 假 DSH 宿主），不使用网络与真实模型；宿主按 `tests/fixtures/m2/recorded_bridge.jsonl` 的录制消息调用桥接端点。覆盖：

- 冻结 `1m`/`5m` 双周期快照后发起框选会诊，得到 `READY` 的 LONG `Setup Insight`，证据含 `snapshot:<id>` 与 `chartview:<consultation_id>:1m`。
- 宿主经桥接写入一条合法标注、一条越界标注、一条预警草案；断言越界标注返回 `422 PRICE_OUT_OF_BOUNDS` 且不落库，草案为 `DRAFT`，确认后转 `ARMED`。
- 采纳标注生成独立的 `HORIZONTAL_LINE` HUMAN_DRAWING（`drawing_id` 独立、价格与标注一致、`snapshot_id` 绑定）。
- 扫描触发 `READY` 简报，断言 `briefing.snapshot_id == trigger.snapshot_id`；`/consult` 返回预填问题并沿用同一快照/标的/周期；已读后从未读列表消失。
- Token 用量按 `(dsh_session_id, dsh_turn_id)` 幂等：会诊 `1200/300/450` + 简报 `800/100/260`，重复上报不累加（总计 `turns=2`、`input=2000`、`cached=400`、`output=710`）。
- 停掉宿主后 `/api/collaboration` 转离线，`/api/health`、`/desk`、Cookie、`1m`/`5m` 切图、`chart-view`、标注、预警与刷新仍可用。

### 发现并修复的缺陷

- `collaboration_service._post_to_host` / `briefing_service._post_to_host` 的 `httpx.AsyncClient` 未隔离环境代理；本机 `NO_PROXY` 含 `[::1]` 时构造客户端抛 `httpx.InvalidURL: Invalid port: ':1]'`，`_run` 未捕获导致会诊永久 `PENDING`。改为 `trust_env=False`，并加 `tests/workbench_api/test_collaboration_api.py::test_host_dispatch_ignores_ambient_http_proxy`（已验证去掉修复会失败）。
- DSH 打印的入口 URL 带 `?token=…`；`_collaboration_host()` 直接返回 `info.url`，拼接后变成 `http://127.0.0.1:<port>/?token=…/talaria/consult`，宿主返回 404、会诊误判 `HOST_OFFLINE`。新增 `DshHostInfo.base_url` 并在派发时使用，`tests/cli/test_dsh_host.py` 增加回归断言。
- 测试自身的启动竞态：`/api/health` 可能早于 `runtime/dsh-bridge.json` 落盘；改为轮询等待凭据文件。
- 页面刷新期间若行情已持续断流，新 WS 订阅不会收到当前健康快照，顶栏一直显示「等待行情」而不是「断流」。修复：`FeedSupervisor` 增加 `has_channel`/`channel_health`，`/ws/desk` 在复用已存在通道时向新订阅补发当前健康快照；回归 `tests/workbench_api/test_feed_ws.py::test_existing_channel_sends_current_health_to_new_subscriber`。浏览器验证：宿主离线 + 断流时刷新，显示「断流 48 秒」、断流提示条与 295 根已加载 K 线。

### §5.3 门禁

- `tests/foundation/test_dsh_tool_schema.py` 原有遍历门禁继续覆盖全部 7 个工具；新增 `write_agent_overlay`（`snapshot_id` 首位、价格字符串、证据子对象闭合、无 `$defs`）与 `create_alert`（无 `draft`/`status` 越权字段、`condition` 闭合）断言。
- `tests/cli/test_talaria_desk_profile.py` 新增 `test_dump_snapshot_is_ci_runnable_gate`：不依赖本机 `dsh` 即可解析 `tests/fixtures/dsh/talaria-desk.dump.snapshot.txt`，断言 preset/plugin 各 1、无 `danger-full-access`、`mode: read-only`、`policy: ask`；真实 `dsh --profile talaria-desk --dump-config` 对照测试在已安装 profile 的环境照常执行（未跳过）。

### 浏览器验收

真实浏览器（ego-browser，Chromium）在 `1440×900` 与 `390×844`、浅色/深色下检查简体中文工作台；另起回放断流守护验证「宿主离线 + 数据源断流」同时发生：

1. `.scratch/tasks/ticket-20/shots/01-desktop-light-workbench.png`
2. `.scratch/tasks/ticket-20/shots/02-desktop-light-briefings.png`
3. `.scratch/tasks/ticket-20/shots/03-desktop-light-collaboration.png`
4. `.scratch/tasks/ticket-20/shots/04-desktop-light-annotations.png`
5. `.scratch/tasks/ticket-20/shots/05-desktop-dark-workbench.png`
6. `.scratch/tasks/ticket-20/shots/06-mobile-390-dark.png`
7. `.scratch/tasks/ticket-20/shots/07-mobile-390-light.png`
8. `.scratch/tasks/ticket-20/shots/08-desktop-light-offline-stale.png`
9. `.scratch/tasks/ticket-20/shots/09-desktop-dark-offline-stale.png`
10. `.scratch/tasks/ticket-20/shots/10-mobile-390-light-offline-stale.png`
11. `.scratch/tasks/ticket-20/shots/11-mobile-390-dark-offline-stale.png`
12. `.scratch/tasks/ticket-20/shots/12-desktop-light-reload-stale.png`（宿主离线 + 断流期间刷新后）

均逐张目视确认非空白；离线断流组可见 `协同宿主：离线 · 快通道正常`、`断流 N 秒` 与 `FEED_GAP` 提示，图表/关键位/扫描面板继续渲染。

### 命令结果

- `uv run pytest -q` → `517 passed, 1 deselected`
- `uv run talaria contract-check` → `契约生成物零漂移。`
- `pnpm -C dsh typecheck` → 通过
- `pnpm -C dsh --filter @talaria/desk-ui test` → `155 passed`
- `pnpm -C dsh --filter @talaria/dsh-plugin test` → `22 passed`
- `pnpm -C dsh build` → 通过（仅既有 500 kB chunk 警告）

### 已知限制

- 真实模型会话冒烟未执行：本沙箱无网络模型凭据，按任务单“沙箱外可选”保留未勾选；DSH Profile、插件与桥接的真实安装/`dump-config` 已在本机验证。
- 浏览器验收使用 ego-browser 注入的截图；断流场景为回放模拟（`outage_hold_60s.jsonl`），未覆盖真实交易所网络黑洞。
- 扫描简报仍沿用既有单进程内存冷却/并发与 200 条窗口限制。
