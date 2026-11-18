# 14 — 关键位预警（`LEVEL_EVENT`）与关键位一键采纳为人工画线

**What to build:** 交易员在 `SYSTEM_LEVEL` 关键位上一键挂预警，按 `level_id` 跟踪触及、突破与回踩；也可把认可的关键位一键采纳复制为 `HUMAN_DRAWING` 画线，采纳操作计入质量反馈。

**Blocked by:** 10 — 关键位引擎第一批与 `SYSTEM_LEVEL` 图层；13 — 盘面预警引擎

**Source of truth:** [需求文档](../spec.md)、[领域词汇表](../../../GLOSSARY.md)、[系统分层架构规范](../../../docs/architecture.md)、[ADR-0008](../../../docs/adr/0008-analysis-first-v1-and-deterministic-key-level-engine.md)

**Status:** ready-for-agent

- [x] 预警引擎新增 `LEVEL_EVENT` 条件：按 `level_id` 跟踪触及、突破、回踩；关键位状态变化（如 `FLIPPED`）后预警按最新状态判定。
- [x] 关键位“采纳”复制为 `HUMAN_DRAWING` 图元（之后与系统关键位独立），采纳记录与 10 的“忽略”一起持久化为质量反馈。
- [x] 离线测试覆盖三种事件判定与采纳记录。
- [x] 浏览器界面验收（均附截图）：宽度 `1440` 下浅色与深色、简体中文检查关键位右键 / 浮层中的“挂预警”“采纳”“忽略”操作；宽度 `390` 下这些操作隐藏、关键位只读。

## Comments

### 实现

- 预警契约：`AlertConditionKind.LEVEL_EVENT`；`AlertLevelEvent`（`TOUCH|BREAK|RETEST`）；`AlertCondition` 新增 `level_id`、`level_event`、`level_kind`，`direction` 改为可选并按条件类型校验形状。
- 引擎：`AlertEngine` 新增可注入的 `key_level_provider`（默认用 `compute_key_levels` 与当前 cutoff 只读计算，按 `(instrument,timeframe,cutoff)` 缓存，不向前看）；复用 13 的布尔上升沿、冷却、单次/重复、`expires_at`、STALE 挂起与恢复重置基线、触发冻结 `snapshot_id`。
- 采纳：`KeyLevelFeedbackService` 通过 `DrawingService` 把关键位复制为 `HUMAN_DRAWING`（水平线/区间矩形），`POST /api/key-levels/{level_id}/adopt`（Bearer）；`key_level_adoptions` 表经 `StateWriterQueue` 写入；重复采纳返回现有画线，画线被删除后允许重新采纳。
- 反馈：`GET /api/key-levels/feedback` 汇总采纳与未撤销的忽略 `level_id`。
- 前端：关键位行尾浮层“挂预警 / 采纳为画线 / 忽略”，采纳后行内“已采纳”；`挂预警` 以 `level_id`、`level_kind` 预填 13 的预警表单（条件 `LEVEL_EVENT`，事件默认 `TOUCH`）；采纳后刷新画线，可从画线管理看到来源文本。

### LEVEL_EVENT 语义

- `TOUCH`：当前 K 线 high/low 覆盖关键位价格或区间即成立，但默认 provider 只看到 cutoff 前的已确认柱，避免用未收盘数据前视。
- `BREAK`：仅 confirmed 收盘穿越，方向由收盘价相对价位/区间上下界推导；关键位须已由引擎判定为 `BROKEN/FLIPPED`，`ACTIVE` 时不触发。
- `RETEST`：初次 confirmed 收盘在关键位另一侧时记录突破方向；之后价格回踩该位并收盘回到突破方向一侧时成立；同一突破-回踩序列只触发一次，重复模式下冷却结束后需重新积累序列；`FLIPPED` 后方向语义按最新价格侧翻转。
- STALE 期间跳过；LIVE 恢复后重置基线与回踩序列，避免恢复瞬间误触发。

### 测试

- 新增 `tests/foundation/test_alerts_level_event.py`（12）：TOUCH 上升沿与冻结快照、冷却后需新边沿、BREAK confirmed、`FLIPPED` 方向、RETEST 回踩一次、`FLIPPED` 反转回踩方向、单次 `EXPIRED`、STALE 跳过与恢复重置、provider cutoff、`level_kind` 不匹配、区间 TOUCH、契约互斥字段。
- 新增 `tests/workbench_api/test_key_level_adoption.py`（5）与 `test_key_level_adoption_api.py`（4）：独立画线、重复采纳、区间矩形、反馈计数、持久化、404/401/422。
- `uv run pytest -q`：402 passed；`uv run talaria contract-check`：零漂移；前端 Vitest 118 passed。

### 截图

- `.scratch/tasks/ticket-14/01-light-level-actions.png`（1440 浅色，关键位操作浮层）
- `.scratch/tasks/ticket-14/02-dark-level-event-form.png`（1440 深色，挂预警表单预填）
- `.scratch/tasks/ticket-14/03-adopted-human-drawing.png`（1440 深色，采纳后画线与已采纳标记）
- `.scratch/tasks/ticket-14/03b-adopted-in-drawing-manager.png`（1440，画线管理中的来源文本）
- `.scratch/tasks/ticket-14/04-390-readonly.png`（390 只读，操作隐藏）
- `.scratch/tasks/ticket-14/05-empty-state.png`（1440 浅色空态）

### 已知限制

- 采纳复制的是关键位当前几何（单线为水平线、区间为矩形），不是持续跟随系统关键位变化的引用。
- 回踩序列状态保存在进程内，服务重启后会从基线重新积累，不会跨重启保留。
- 默认 provider 每个 cutoff 重新计算一次关键位并缓存，关键位数量较多时大盘会有额外计算开销。
- 画线管理文本沿用既有单行截断样式，超长 `level_id` 在窄面板内以省略/裁剪呈现，不撑破布局。
- 前端自动化测试仍为纯 model 测试（仓库未装 DOM 测试库），交互与 390 只读以浏览器截图验收。
