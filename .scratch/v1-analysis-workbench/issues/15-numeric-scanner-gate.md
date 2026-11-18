# 15 — 确定性异动扫描器（`Scanner`，纯数值，零模型调用）

**What to build:** 守护进程持续扫描 `Watchlist` 的流动性、波动率扩张、资金费率极端分位、持仓量背离与高强度关键位触及 / 突破；命中标的归入 `SCANNER_FOCUS` 分组，冻结快照生成 `Trigger` 并在工作台推送异动卡片。`--solo` 下完整可用；协同简报唤醒由 19 接入。

**Blocked by:** 10 — 关键位引擎第一批与 `SYSTEM_LEVEL` 图层；13 — 盘面预警引擎

**Source of truth:** [需求文档](../spec.md)、[领域词汇表](../../../GLOSSARY.md)、[系统分层架构规范](../../../docs/architecture.md)、[ADR-0004](../../../docs/adr/0004-dual-mode-intraday-cognition-and-scanner-gate.md)、[ADR-0008](../../../docs/adr/0008-analysis-first-v1-and-deterministic-key-level-engine.md)

**Status:** ready-for-agent

- [ ] 向量化扫描面向 `MarketEventFeed`，不调用任何模型；`STALE` 标的跳过。
- [ ] 命中时冻结快照生成 `Trigger`，经 `TriggerRoutingPolicy` 路由；本任务只实现本地工作台提醒通道，`CO_PILOT_CONSULT` 通道留接口。按 `Instrument` 冷却，避免同一异动重复刷屏。
- [ ] 命中标的动态写入 `SCANNER_FOCUS` 分组，异动消退后按规则移出。
- [ ] 看盘页提供异动卡片与列表，点击切换主图到对应 `Instrument`、周期与触发快照视窗。
- [ ] 离线测试覆盖各异动条件、冷却、`STALE` 跳过，并断言扫描路径无模型调用。
- [ ] 浏览器界面验收（均附截图）：宽度 `1440` 与 `390`、浅色与深色、简体中文下检查 `SCANNER_FOCUS` 分组与异动列表；覆盖多指标异动长摘要与多标签折叠，以及平静盘面无命中的空态。
