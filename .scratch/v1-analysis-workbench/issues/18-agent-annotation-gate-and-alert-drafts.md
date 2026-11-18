# 18 — 智能体图表标注（`AGENT_ANNOTATION`）硬门控与智能体预警草案

**What to build:** 智能体在会诊中向主图写建议标注、提议预警草案；Python 底座对标注执行快照匹配、证据引用、最小变动价位与 ATR 包络等硬门控，拦截幻觉价位。交易员在独立 `AGENT_ANNOTATION` 图层审阅标注，可一键采纳为人工实线、按会话过滤或批量清理，并在待审区确认或废弃智能体提议的预警。

**Blocked by:** 13 — 盘面预警引擎（草案态已实现）；17 — 协同会诊

**Source of truth:** [需求文档](../spec.md)、[领域词汇表](../../../GLOSSARY.md)、[系统分层架构规范](../../../docs/architecture.md)、[ADR-0001](../../../docs/adr/0001-dual-plane-dsh-and-python-core.md)、[ADR-0005](../../../docs/adr/0005-fast-path-bypass-and-decoupled-chart-workbench.md)、[ADR-0008](../../../docs/adr/0008-analysis-first-v1-and-deterministic-key-level-engine.md)

**Status:** ready-for-agent

- [ ] 桥接开放 `write_agent_overlay` 与草案态 `create_alert` 工具（智能体只能创建草案）。
- [ ] `write_agent_overlay` 任一条件不满足即拒绝并返回结构化错误码：① `snapshot_id` 存在且属于同一 `Instrument`，`evidence_refs` 非空且引用该快照中的 `level_id` 或具体 K 线时刻；② 价格为正、对齐 `tick_size`、落在 `[min_low - 3 * ATR_14, max_high + 3 * ATR_14]` 内；③ 时间锚点不早于快照起点，向未来推演不超过当前周期 100 根 K 线。
- [ ] 单一 `Instrument` 未采纳的活跃标注不超过 12 条，超出时按轮次归档旧标注。
- [ ] `AGENT_ANNOTATION` 图层以带智能体角标的虚线 / 半透明样式渲染并禁止拖拽；点击查看 `snapshot_id`、轮次与 `evidence_refs`；支持采纳为 `HUMAN_DRAWING`、按会话过滤与批量清理。
- [ ] 智能体预警草案实时推送到待审区，确认后转 `ARMED`，废弃后归档且绝不触发。
- [ ] 离线测试覆盖每条门控的拒绝码、12 条上限与草案确认 / 废弃。
- [ ] 浏览器界面验收（均附截图）：宽度 `1440` 下浅色与深色、简体中文检查标注渲染、溯源浮层、采纳 / 清理控件与草案卡片，覆盖长溯源说明与多条证据引用不遮挡、无标注无草案的空态；宽度 `390` 下检查标注只读显示与草案卡片可读。
