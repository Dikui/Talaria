# 18 — 智能体图表标注（`AGENT_ANNOTATION`）硬门控与智能体预警草案

**What to build:** 智能体在会诊中向主图写建议标注、提议预警草案；Python 底座对标注执行快照匹配、证据引用、最小变动价位与 ATR 包络等硬门控，拦截幻觉价位。交易员在独立 `AGENT_ANNOTATION` 图层审阅标注，可一键采纳为人工实线、按会话过滤或批量清理，并在待审区确认或废弃智能体提议的预警。

**Blocked by:** 13 — 盘面预警引擎（草案态已实现）；17 — 协同会诊

**Source of truth:** [需求文档](../spec.md)、[领域词汇表](../../../GLOSSARY.md)、[系统分层架构规范](../../../docs/architecture.md)、[ADR-0001](../../../docs/adr/0001-dual-plane-dsh-and-python-core.md)、[ADR-0005](../../../docs/adr/0005-fast-path-bypass-and-decoupled-chart-workbench.md)、[ADR-0008](../../../docs/adr/0008-analysis-first-v1-and-deterministic-key-level-engine.md)

**Status:** resolved

- [x] 桥接开放 `write_agent_overlay` 与草案态 `create_alert` 工具（智能体只能创建草案）。
- [x] `write_agent_overlay` 任一条件不满足即拒绝并返回结构化错误码：① `snapshot_id` 存在且属于同一 `Instrument`，`evidence_refs` 非空且引用该快照中的 `level_id` 或具体 K 线时刻；② 价格为正、对齐 `tick_size`、落在 `[min_low - 3 * ATR_14, max_high + 3 * ATR_14]` 内；③ 时间锚点不早于快照起点，向未来推演不超过当前周期 100 根 K 线。
- [x] 单一 `Instrument` 未采纳的活跃标注不超过 12 条，超出时按轮次归档旧标注。
- [x] `AGENT_ANNOTATION` 图层以带智能体角标的虚线 / 半透明样式渲染并禁止拖拽；点击查看 `snapshot_id`、轮次与 `evidence_refs`；支持采纳为 `HUMAN_DRAWING`、按会话过滤与批量清理。
- [x] 智能体预警草案实时推送到待审区，确认后转 `ARMED`，废弃后归档且绝不触发。
- [x] 离线测试覆盖每条门控的拒绝码、12 条上限与草案确认 / 废弃。
- [x] 浏览器界面验收（均附截图）：宽度 `1440` 下浅色与深色、简体中文检查标注渲染、溯源浮层、采纳 / 清理控件与草案卡片，覆盖长溯源说明与多条证据引用不遮挡、无标注无草案的空态；宽度 `390` 下检查标注只读显示与草案卡片可读。

## Comments

- **实现位置**：门控与契约在 `core/src/talaria/foundation/annotations/`（`contracts.py`、`gate.py`、`tools.py`），用例层在 `core/src/talaria/workbench_api/annotation_service.py`；`agent_annotations` / `annotation_adoptions` 由 `StateWriterQueue` 单写。DSH 桥新增 `write_agent_overlay`、`create_alert`（`dsh/packages/talaria-dsh-plugin/`，schema 由 `talaria contract-check --write` 生成），前端在 `dsh/packages/talaria-desk-ui/src/features/annotations/`。
- **门控码（HTTP 422）**：`SNAPSHOT_MISMATCH`（快照不存在、跨标的、周期不在快照内）、`EVIDENCE_EMPTY`、`EVIDENCE_UNRESOLVED`（`level_id` 不在该快照关键位集合，或 K 线时刻超出 ±1 根容差）、`PRICE_INVALID`（价格 ≤ 0）、`PRICE_NOT_TICK_ALIGNED`、`PRICE_OUT_OF_BOUNDS`（窗口极值 ±3×ATR_14；ATR_14 由 Python 算子基于快照窗口计算，FEED_GAP 不参与，窗口不足 14 根时按无界拒绝）、`TIME_BEFORE_SNAPSHOT`、`TIME_TOO_FAR_FUTURE`（以窗口最后一根为基准，未来最多 100 根）。门控顺序固定为快照 → 证据 → 价格 → 时间，失败即整条拒绝，不部分写入。
- **12 条上限**：按标的统计 `ACTIVE`（未采纳）标注；插入第 13 条时先按 `round`、`created_ts`、`annotation_id` 归档最旧标注为 `ARCHIVED` 并写归档原因，审计记录保留，默认列表仍返回 12 条活跃标注；`include_archived=true` 可查归档。
- **草案语义**：`create_alert` 工具 schema 不含 `draft`/`status`，桥强制 `draft: true` 并校验服务端返回 `DRAFT`；确认走 `POST /api/alerts/{id}/confirm` 转 `ARMED`，废弃走 `POST /api/alerts/{id}/dismiss` 转 `DISABLED`（保留记录、不触发，视作归档）；草案态不参与触发评估。
- **测试**：`tests/foundation/test_annotations_gate.py`（17，逐条拒绝码、ATR 包络边界含 FEED_GAP、100 根时间边界、level/kline 证据）；`tests/workbench_api/test_annotations_api.py`（16，Bearer、持久化与过滤、12 条归档、采纳生成独立 `HUMAN_DRAWING`、忽略/清理、草案确认与废弃不触发）；`features/annotations/model.test.ts`（7，含 390 只读隐藏操作）；`tests/foundation/test_dsh_tool_schema.py` 覆盖新工具子集与无根 `$schema`/无 `$defs`。
- **命令结果**：`uv run pytest -q` 493 passed / 1 deselected；`uv run talaria contract-check` 零漂移；`pnpm -C dsh typecheck` 通过；`pnpm -C dsh --filter @talaria/desk-ui test` 145 passed；`pnpm -C dsh build` 通过；`pnpm -C dsh --filter @talaria/dsh-plugin test` 21 passed。
- **截图**（本机绝对路径，未提交）：`/Users/dikui/Project/Talaria/.agents/worktrees/ticket-18-agent-annotations/.scratch/screens/ticket-18/1440-light-annotations.png`、`1440-light-provenance.png`、`1440-light-long-provenance.png`（长溯源 + 5 条证据滚动不遮挡）、`1440-light-adopt.png`、`1440-light-draft.png`、`1440-dark-annotations.png`、`1440-light-empty.png`、`390-readonly.png`、`390-readonly-panel.png`。
- **已知限制**：① 390 宽下的标注面板在页面下方，需滚动；只读断言覆盖采纳/忽略/批量清理按钮数量为 0。② 独立 `/desk` 页在 900px 高窗口内图表区高于视口，低价位标注需滚动查看；这是既有布局行为，本单未改全局布局。③ 预警“废弃”沿用 13 的 `DISABLED` 语义，记录保留在已过期页签而非物理删除。④ 采纳时先建人工画线再更新标注状态，两步非同一事务；若第二步失败会留下孤立人工画线。⑤ 截图为 Playwright 无头 Chromium 生成（ego 浏览器视口固定且截图带光标浮层）。


### Lead 验收

- 合并 `99c8bfa Merge branch 'ticket-18-agent-annotations'`（自动合并）；合并后 `uv run pytest -q` → 496 passed, 1 deselected；`uv run talaria contract-check` 零漂移；`pnpm -C dsh typecheck` 通过；desk-ui 145 passed（20 files）；plugin 21 passed；`pnpm -C dsh build` 通过。
- 目视 9 张截图：1440 浅色标注图层与右侧 5 条标注（活跃/已采纳/证据）、长溯源固定浮层滚动、采纳后生成人工画线、草案待审区、深色、空态、390 只读均符合验收。
- 复核：8 个拒绝码均映射 422；12 条上限只统计 ACTIVE，第 13 条按 round/created_ts 归档；`create_alert` schema 强制 draft，桥不接受 status/draft；标注写路径经 StateWriterQueue；草案确认转 ARMED、废弃 DISABLED 不触发。
- 接受已知限制：390 标注面板需向下滚动；独立 `/desk` 900px 高时低价位标注需滚动；采纳“建画线→更新标注”两步非事务，极端失败会留孤立画线（已记录）；截图为 Playwright 无头 Chromium 生成（非 ego），但为真实浏览器渲染，已目视核对。
