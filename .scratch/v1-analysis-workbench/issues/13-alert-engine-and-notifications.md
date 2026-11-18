# 13 — 盘面预警引擎（`Alert`）：阈值 / 指标信号 / 画线突破与工作台提醒

**What to build:** 交易员创建价格、盘口、资金费率、持仓量阈值预警、指标信号预警，或在人工画线上一键挂预警；条件满足时冻结 `Evidence Snapshot` 生成 `Trigger`，工作台毫秒级弹窗、列表高亮并可选声音提醒，点击触发记录定位到对应盘面。`LEVEL_EVENT` 由 14 加入。

**Blocked by:** 05 — 人工画线持久化、撤销重做与视窗回读；07 — 永续衍生品数据与衍生品指标；08 — 自选观察池与实时订阅优先级

**Source of truth:** [需求文档](../spec.md)、[领域词汇表](../../../GLOSSARY.md)、[系统分层架构规范](../../../docs/architecture.md)、[ADR-0005](../../../docs/adr/0005-fast-path-bypass-and-decoupled-chart-workbench.md)、[ADR-0008](../../../docs/adr/0008-analysis-first-v1-and-deterministic-key-level-engine.md)

**Status:** ready-for-agent

- [x] 引擎面向 `MarketEventFeed` 运行，同一套触发与冷却代码适配实时热流与冷库事件回放；支持 `METRIC_THRESHOLD`、`INDICATOR_SIGNAL`（复用 Python 算子）与 `OVERLAY_CROSS`（画线几何插值触碰或收盘突破，改线后按最新锚点生效、无需重建预警）。条件类型可扩展。
- [x] 状态机 `ARMED → COOLDOWN / EXPIRED`：触发时冻结快照生成带 `snapshot_id` 的 `Trigger`；单次预警转 `EXPIRED`，重复预警冷却到期恢复 `ARMED`；`Instrument` 处于 `STALE` 时挂起，恢复瞬间不误报。
- [x] `create_alert` 支持直接创建激活态与创建草案态（草案经人工确认才转 `ARMED`）；草案态在此任务一次实现，18 只负责把智能体接入。
- [x] 活跃预警把所属 `Instrument` 提升到订阅最高优先级（接入 08 的调度规则）。
- [x] 看盘页支持在画线上一键挂预警、预警规则列表（活跃 / 冷却 / 已过期 / 草案）与触发历史。
- [x] 离线测试覆盖三类条件、状态机、`STALE` 挂起与回放一致性。
- [x] 浏览器界面验收（均附截图）：宽度 `1440` 下浅色与深色、简体中文检查预警表单、图上挂预警菜单、规则列表与触发通知卡，覆盖长备注换行与无规则无历史的空态；宽度 `390` 下检查规则列表与通知卡可读，创建入口可隐藏。

## Comments

**实现（分支 `ticket-13-alert-engine`，基线 `5afa649`）**

- 引擎：`core/src/talaria/foundation/alerts/`（`contracts.py` 契约、`engine.py` 纯逻辑、`overlay.py` 画线几何插值）。`AlertEngine` 只消费 `MarketEventFeed` 语义的 `FeedEvent`/`FeedHealthEvent`，时钟、历史窗口、快照冻结均以回调注入，实时 `FeedSupervisor` 与 `ReplayFeed` 走同一套触发/冷却代码。
- 条件语义：
  - `METRIC_THRESHOLD`：`last_price` 用 K 线收盘价（未确认柱亦可），`bid_price`/`ask_price` 用盘口，`mark_price`/`funding_rate`/`open_interest` 用 `DTickEvent`；`ABOVE` = 观测值 ≥ 阈值，`BELOW` = 观测值 ≤ 阈值。
  - `INDICATOR_SIGNAL`：复用 `foundation/operators.compute_series`，只在 confirmed K 线求值；可用“指标通道 vs 常数阈值”或“与另一指标对比”两种比较，禁止前端数学。
  - `OVERLAY_CROSS`：按画线最新 `points` 求当前柱时间窗的上下沿，`TOUCH` 用 high/low 触碰，`CLOSE_BREAK` 用 confirmed 收盘价突破；水平线/射线/线段/斐波那契/矩形/平行通道可用，文本标签不可挂。画线修改变更几何后无需重建预警。
  - 所有条件都用“上穿/下穿”的布尔边沿：首次求值、冷却结束、断流恢复后的第一次求值只建立基线，不触发。
- 状态机：`DRAFT → ARMED → (触发) → COOLDOWN → ARMED`，单次触发或 `repeat=false` 进入 `EXPIRED`，`DISABLED` 可随时停用/启用；`expires_at` 到期转 `EXPIRED`。触发时先按触发时刻 `cutoff_ts` 冻结 Evidence Snapshot，`AlertTriggerRecord.snapshot_id` 持久化；`STALE`/`RECOVERING` 期间挂起求值，恢复后跳过断流回填柱并重置基线。
- 持久化：`state/collaboration_state.sqlite` 新增 `alerts`、`alert_triggers` 表，全部经 `StateWriterQueue.run` 单写；触发写入与规则状态更新在同一串行队列内完成，保留既有 05/08/10 表。
- API：`GET/POST /api/alerts`、`GET /api/alerts/triggers`、`GET/PATCH/DELETE /api/alerts/{id}`、`POST /api/alerts/{id}/confirm`、`POST /api/alerts/{id}/dismiss`；草案需 confirm 才 `ARMED`，草案永不触发；写操作校验 Bearer，错误走结构化 `ErrorResponse`。
- 订阅优先级：`AlertService.active_instrument_ids()` 注入 `WatchlistService`，再传给 `RealtimeSubscriptionScheduler.reconcile(..., alerted=)`，顺序为“图表 > 活跃预警 > 普通自选”；删除/过期/冷却（冷却期间不再是活跃触发风险，但仍属活跃规则）由 `active_instrument_ids` 的 `ARMED` 过滤控制，quota 保证订阅数有上限。
- 前端：`features/alerts/`（`model.ts` 纯函数 + `useAlerts.ts` 状态/轮询/WS 注入 + `AlertPanel.tsx`），画线属性浮层新增“挂预警”入口，打开预填 `OVERLAY_CROSS` 的表单；规则页签（活跃/冷却/已过期/草案）、触发历史、toast/通知卡、点击定位到触发时刻快照与标的周期、声音开关默认关；空态、长备注 `pre-wrap`、1440 浅/深与 390 布局已适配。

**测试**

- `tests/foundation/test_alerts_engine.py`：12 项，覆盖三类条件、边沿/冷却/单次/过期、STALE 挂起与恢复不误报、草案不触发、画线改几何后取最新、ReplayFeed 与实时一致。
- `tests/workbench_api/test_alert_service.py`：5 项，覆盖草案确认、触发落库与 snapshot_id、冷却/禁用/删除释放订阅、重启恢复、编辑阈值生效。
- `tests/workbench_api/test_alerts_api.py`：7 项，覆盖 CRUD、confirm/dismiss、画线与指标校验、Bearer 鉴权。
- 前端 `features/alerts/model.test.ts`：13 项，覆盖相对时间、页签映射/排序、表单校验与 payload、条件摘要；`features/feed/reducer.test.ts` 与 `messages.test.ts` 各补 1 项 alert 帧覆盖。
- 命令结果：`uv run pytest -q` → 348 passed, 1 deselected；`uv run talaria contract-check` → 契约生成物零漂移；`pnpm -C dsh typecheck` 通过；`pnpm -C dsh --filter @talaria/desk-ui test` → 16 files / 112 tests passed；`pnpm -C dsh build` 通过（仅 chunk 体积告警）。

**截图（绝对路径，未提交仓库）**

- `/tmp/talaria-shots/01-1440-light-toast.png`：1440 浅色，触发通知卡 + 规则列表。
- `/tmp/talaria-shots/02-1440-light-list.png`：1440 浅色规则列表与触发历史。
- `/tmp/talaria-shots/03-1440-light-form.png`：1440 浅色预警表单。
- `/tmp/talaria-shots/04-1440-dark-form.png`：1440 深色预警表单。
- `/tmp/talaria-shots/05-1440-dark-long-note.png`：1440 深色已过期列表，长备注换行。
- `/tmp/talaria-shots/06-1440-drawing-attach.png`：画线属性浮层“挂预警”入口。
- `/tmp/talaria-shots/07-1440-drawing-attach-form.png`：一键挂载后预填 `OVERLAY_CROSS` 的表单。
- `/tmp/talaria-shots/08-390-light-list.png`：390 规则列表（创建入口隐藏）。
- `/tmp/talaria-shots/09-1440-empty-state.png`：无规则、无触发历史空态。
- `/tmp/talaria-shots/10-390-light-toast.png`：390 触发通知卡可读。

**已知限制**

- 画线在视窗外时不会出现在“画线管理”列表，但预警仍按几何生效（本次用当前视窗内画线演示挂载）。
- `OVERLAY_CROSS` 的画线几何按当前窗口时间插值，尚未处理画线被软删除后重新恢复的版本切换语义（恢复后按最新版本几何生效）。
- 前端触发通知历史每次轮询刷新最近 50 条，未做分页；规则列表一次加载全部规则。
- 修复了 `styles.css` 中既有的未闭合 `@media (max-width: 720px)` 块（该 bug 会把其后的宿主徽标、关键位与新增预警样式全部吞进移动端媒体查询）。
