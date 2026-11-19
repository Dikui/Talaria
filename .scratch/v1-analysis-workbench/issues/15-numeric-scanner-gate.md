# 15 — 确定性异动扫描器（`Scanner`，纯数值，零模型调用）

**What to build:** 守护进程持续扫描 `Watchlist` 的流动性、波动率扩张、资金费率极端分位、持仓量背离与高强度关键位触及 / 突破；命中标的归入 `SCANNER_FOCUS` 分组，冻结快照生成 `Trigger` 并在工作台推送异动卡片。`--solo` 下完整可用；协同简报唤醒由 19 接入。

**Blocked by:** 10 — 关键位引擎第一批与 `SYSTEM_LEVEL` 图层；13 — 盘面预警引擎

**Source of truth:** [需求文档](../spec.md)、[领域词汇表](../../../GLOSSARY.md)、[系统分层架构规范](../../../docs/architecture.md)、[ADR-0004](../../../docs/adr/0004-dual-mode-intraday-cognition-and-scanner-gate.md)、[ADR-0008](../../../docs/adr/0008-analysis-first-v1-and-deterministic-key-level-engine.md)

**Status:** resolved

- [x] 向量化扫描面向 `MarketEventFeed`，不调用任何模型；`STALE` 标的跳过。
- [x] 命中时冻结快照生成 `Trigger`，经 `TriggerRoutingPolicy` 路由；本任务只实现本地工作台提醒通道，`CO_PILOT_CONSULT` 通道留接口。按 `Instrument` 冷却，避免同一异动重复刷屏。
- [x] 命中标的动态写入 `SCANNER_FOCUS` 分组，异动消退后按规则移出。
- [x] 看盘页提供异动卡片与列表，点击切换主图到对应 `Instrument`、周期与触发快照视窗。
- [x] 离线测试覆盖各异动条件、冷却、`STALE` 跳过，并断言扫描路径无模型调用。
- [x] 浏览器界面验收（均附截图）：宽度 `1440` 与 `390`、浅色与深色、简体中文下检查 `SCANNER_FOCUS` 分组与异动列表；覆盖多指标异动长摘要与多标签折叠，以及平静盘面无命中的空态。

## Comments

### 实现
- 纯数值扫描器位于 `core/src/talaria/foundation/scanner/`：`contracts.py` 定义 Trigger / Evidence / Focus / Scan 请求响应；`engine.py` 维护已确认、非 `FEED_GAP` 的 1m 缓冲，接收 `KlineEvent` / `FeedHealthEvent` / `DerivativeTickEvent`，不导入任何模型 SDK；`routing.py` 定义 `TriggerRoutingPolicy` 与 `CoPilotConsultChannel` 接口。
- 五类条件与默认阈值（`ScannerConfig`，全部可配）：流动性异常（成交额/成交量 60 根滚动 z≥2.5 或分位≥98%）；波动率扩张（真实波幅相对 14 根 ATR 倍数≥1.5 或 60 根分位≥95%）；资金费率极端（最近 90 次结算、至少 24 次，分位≥95%/≤5%，超过 36h 跳过）；OI 背离（6 根窗口、价格变动≥0.5% 且 OI 反向变动≥1%）；关键位事件（复用 10/11 的 `compute_key_levels`，`strength>=0.7`，仅确认柱后判定触及/突破）。窗口、分位、冷却和焦点 TTL 均在 `ScannerConfig` 中。
- 跳过 `STALE` 标的与最后缓冲之后的 `FEED_GAP`；同一根 bar 只评估一次，重复扫描不会重复生成 Trigger；`trigger_id=scn_<sha1(instrument|timeframe|kind|bar_ts)[:16]>`，相同输入可复现。
- 命中先由快通道按 `(instrument, now)` 冻结一次 Evidence Snapshot，同一轮多类命中共用同一个 `snapshot_id`；冻结失败/空快照不产生 Trigger。`ScannerTrigger` 含 instrument/timeframe/kind/severity/title/summary/evidence/tags/created_ts/snapshot_id/route。
- 路由默认 `LOCAL`（服务端历史 + WebSocket 推送），`CO_PILOT_CONSULT` 仅通过 `TriggerRoutingPolicy` 的注入接口占位，默认关闭，不调用模型、不发网络请求；`ScannerService` 中没有任何 `openai`/`anthropic`/`litellm`/`talaria.cognition` 导入。
- 冷却按 `(instrument_id, kind)` 独立计时，默认 5 分钟，冷却内同类异动不重复生成；不同类型可分别触发。
- `SCANNER_FOCUS` 由 `ScannerService` 在活跃标的集合变化时通过 `StateWriterQueue.run(upsert_system_group("扫描焦点", ids))` 持久化；焦点 TTL 默认 30 分钟，无活跃标的时保留系统分组但清空成员，不触碰用户手工分组。`ScannerFocusItem` 含最近类型/时间/快照信息。
- API：`GET /api/scanner/triggers`（limit/instrument_id/kind/severity/snapshot_id 过滤）、`GET /api/scanner/focus`、`POST /api/scanner/scan`（可选 instrument_ids，写操作沿用 Bearer 校验）；`/ws/desk` 新增 `{"type":"scanner","data":...}`。`--solo` 下由 fixture 预热历史、派生品序列和 3s tick 完整驱动。
- 前端 `features/scanner/`：`ScannerPanel` 以底部 dock 与右侧 `features/alerts/` 预警面板并存，`异动 N` 头部开关；卡片显示 kind/severity/摘要/tags/相对时间，长摘要 2 行截断并可展开证据，多标签自动换行；焦点 chips 与自选池的只读“扫描焦点”系统分组可见；点击卡片/焦点 chip 复用 `locateTrigger` 切换 Instrument/timeframe 并定位到触发快照；空态、加载态、手动扫描与只读（390 隐藏手动扫描）均已实现。`messages.ts`/`reducer.ts` 接入 `scanner` WS 消息。

### 测试与验证
- `uv run pytest -q tests/foundation/test_scanner.py tests/workbench_api/test_scanner_service.py tests/workbench_api/test_scanner_api.py`：18 passed。
- `uv run pytest -q`：399 passed，1 deselected（online）。
- `uv run talaria contract-check`：契约生成物零漂移（新增 7 个 scanner schema 与 `generated.ts` 导出）。
- `pnpm run typecheck`（talaria-desk-ui）：通过。
- `pnpm test`（talaria-desk-ui）：18 files / 126 tests passed，含卡片字段、长摘要折叠、空态、只读、点击 payload、WS 解析。
- `pnpm run build`：通过（仅 Vite chunk-size 提示）。
- 浏览器手工核对：卡片点击后标的切至 ETH、周期 1m，并出现“触发时刻 · 返回最新”；手动扫描返回 200 且不重复生成。

### 截图
- `/Users/dikui/Project/Talaria/.agents/worktrees/ticket-15-numeric-scanner/.scratch/v1-analysis-workbench/screenshots/ticket-15/1440-light-cards.png`
- `/Users/dikui/Project/Talaria/.agents/worktrees/ticket-15-numeric-scanner/.scratch/v1-analysis-workbench/screenshots/ticket-15/1440-dark-cards.png`
- `/Users/dikui/Project/Talaria/.agents/worktrees/ticket-15-numeric-scanner/.scratch/v1-analysis-workbench/screenshots/ticket-15/1440-light-multitag-fold.png`
- `/Users/dikui/Project/Talaria/.agents/worktrees/ticket-15-numeric-scanner/.scratch/v1-analysis-workbench/screenshots/ticket-15/1440-calm-empty.png`
- `/Users/dikui/Project/Talaria/.agents/worktrees/ticket-15-numeric-scanner/.scratch/v1-analysis-workbench/screenshots/ticket-15/1440-light-scanner-focus.png`
- `/Users/dikui/Project/Talaria/.agents/worktrees/ticket-15-numeric-scanner/.scratch/v1-analysis-workbench/screenshots/ticket-15/390-readonly.png`
- 截图未提交入库（`.scratch/.../screenshots/` 保持未跟踪）；平静空态通过浏览器层空数据响应渲染（fixture 样本本身会合法触发关键位/波动率条件），未改动后端逻辑。

### 已知限制
- Trigger 历史目前保存在 `ScannerService` 内存（最多 200 条），进程重启后清空；`SCANNER_FOCUS` 分组本身持久化。后续如需要跨重启审计，可在 `state_store.py` 增加 scanner 表。
- 默认扫描周期为 1m；其他周期需在配置中切换。OI 使用 5m 序列与 1m tick 桶，衍生品历史不足时按规则跳过而非估算。
- `CO_PILOT_CONSULT` 仅有路由接口与占位测试，模型唤醒留给任务 19；本任务不产生任何模型调用。
- 实盘长连接冷启动时，关键位/波动率条件需要先积累足够确认柱；fixture 模式通过启动预热覆盖。


### Lead 验收

- 合并 `6de0069 Merge branch 'ticket-15-numeric-scanner'`；`dsh/packages/talaria-desk-ui/src/lib/api.ts` 与 `styles.css` 与 14 冲突，已保留扫描器与关键位采纳双方方法/样式；main 重跑 `uv run pytest -q` → 420 passed, 1 deselected；`uv run talaria contract-check` 零漂移；`pnpm -C dsh typecheck` 通过；desk-ui 130 passed（18 files）；`pnpm -C dsh build` 通过（580.44 kB JS / 46.23 kB CSS，仅 chunk 提示）。
- 目视截图：1440 浅色底部异动 dock（焦点 chips、严重度、证据、标签、展开摘要）、1440 深色列表、多标签折叠、SCANNER_FOCUS 分组、平静空态、390 只读均符合验收；点击卡片切 ETH/USDT 并定位触发快照。
- 代码审查：`foundation/scanner/` 纯数值 `contracts/engine/routing`，五类条件均跳过 STALE/FEED_GAP，按 `(instrument, kind)` 冷却，同轮共享一次快照冻结；`TriggerRoutingPolicy` 本任务仅 LOCAL，`CO_PILOT_CONSULT` 留注入接口且无模型/网络调用；`ScannerService` 经 `upsert_system_group("扫描焦点", ids)` 写 SCANNER_FOCUS，TTL 消退清空成员保留分组，不破坏用户分组。
- 接受已知限制：Trigger 历史为进程内 200 条；默认扫描 1m；资金费率/OI 历史不足跳过不估算；实盘冷启动需先积累确认柱。
