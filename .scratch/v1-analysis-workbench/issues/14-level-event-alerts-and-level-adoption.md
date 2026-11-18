# 14 — 关键位预警（`LEVEL_EVENT`）与关键位一键采纳为人工画线

**What to build:** 交易员在 `SYSTEM_LEVEL` 关键位上一键挂预警，按 `level_id` 跟踪触及、突破与回踩；也可把认可的关键位一键采纳复制为 `HUMAN_DRAWING` 画线，采纳操作计入质量反馈。

**Blocked by:** 10 — 关键位引擎第一批与 `SYSTEM_LEVEL` 图层；13 — 盘面预警引擎

**Source of truth:** [需求文档](../spec.md)、[领域词汇表](../../../GLOSSARY.md)、[系统分层架构规范](../../../docs/architecture.md)、[ADR-0008](../../../docs/adr/0008-analysis-first-v1-and-deterministic-key-level-engine.md)

**Status:** ready-for-agent

- [ ] 预警引擎新增 `LEVEL_EVENT` 条件：按 `level_id` 跟踪触及、突破、回踩；关键位状态变化（如 `FLIPPED`）后预警按最新状态判定。
- [ ] 关键位“采纳”复制为 `HUMAN_DRAWING` 图元（之后与系统关键位独立），采纳记录与 10 的“忽略”一起持久化为质量反馈。
- [ ] 离线测试覆盖三种事件判定与采纳记录。
- [ ] 浏览器界面验收（均附截图）：宽度 `1440` 下浅色与深色、简体中文检查关键位右键 / 浮层中的“挂预警”“采纳”“忽略”操作；宽度 `390` 下这些操作隐藏、关键位只读。
