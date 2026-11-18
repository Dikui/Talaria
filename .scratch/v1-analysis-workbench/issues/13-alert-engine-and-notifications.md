# 13 — 盘面预警引擎（`Alert`）：阈值 / 指标信号 / 画线突破与工作台提醒

**What to build:** 交易员创建价格、盘口、资金费率、持仓量阈值预警、指标信号预警，或在人工画线上一键挂预警；条件满足时冻结 `Evidence Snapshot` 生成 `Trigger`，工作台毫秒级弹窗、列表高亮并可选声音提醒，点击触发记录定位到对应盘面。`LEVEL_EVENT` 由 14 加入。

**Blocked by:** 05 — 人工画线持久化、撤销重做与视窗回读；07 — 永续衍生品数据与衍生品指标；08 — 自选观察池与实时订阅优先级

**Source of truth:** [需求文档](../spec.md)、[领域词汇表](../../../GLOSSARY.md)、[系统分层架构规范](../../../docs/architecture.md)、[ADR-0005](../../../docs/adr/0005-fast-path-bypass-and-decoupled-chart-workbench.md)、[ADR-0008](../../../docs/adr/0008-analysis-first-v1-and-deterministic-key-level-engine.md)

**Status:** ready-for-agent

- [ ] 引擎面向 `MarketEventFeed` 运行，同一套触发与冷却代码适配实时热流与冷库事件回放；支持 `METRIC_THRESHOLD`、`INDICATOR_SIGNAL`（复用 Python 算子）与 `OVERLAY_CROSS`（画线几何插值触碰或收盘突破，改线后按最新锚点生效、无需重建预警）。条件类型可扩展。
- [ ] 状态机 `ARMED → COOLDOWN / EXPIRED`：触发时冻结快照生成带 `snapshot_id` 的 `Trigger`；单次预警转 `EXPIRED`，重复预警冷却到期恢复 `ARMED`；`Instrument` 处于 `STALE` 时挂起，恢复瞬间不误报。
- [ ] `create_alert` 支持直接创建激活态与创建草案态（草案经人工确认才转 `ARMED`）；草案态在此任务一次实现，18 只负责把智能体接入。
- [ ] 活跃预警把所属 `Instrument` 提升到订阅最高优先级（接入 08 的调度规则）。
- [ ] 看盘页支持在画线上一键挂预警、预警规则列表（活跃 / 冷却 / 已过期 / 草案）与触发历史。
- [ ] 离线测试覆盖三类条件、状态机、`STALE` 挂起与回放一致性。
- [ ] 浏览器界面验收（均附截图）：宽度 `1440` 下浅色与深色、简体中文检查预警表单、图上挂预警菜单、规则列表与触发通知卡，覆盖长备注换行与无规则无历史的空态；宽度 `390` 下检查规则列表与通知卡可读，创建入口可隐藏。
