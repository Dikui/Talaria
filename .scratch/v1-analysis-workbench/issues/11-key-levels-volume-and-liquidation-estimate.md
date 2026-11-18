# 11 — 关键位第二批：成交量位、锚定 VWAP 与估算清算密集区

**What to build:** `SYSTEM_LEVEL` 图层新增 `POC / VAH / VAL`、锚定 VWAP 及标准差带，以及基于持仓量变化与杠杆假设推算的清算密集区；清算密集区显式标为 `ESTIMATED`，在视觉上与观测类关键位分开。

**Blocked by:** 07 — 永续衍生品数据与衍生品指标；10 — 关键位引擎第一批与 `SYSTEM_LEVEL` 图层

**Source of truth:** [需求文档](../spec.md)、[领域词汇表](../../../GLOSSARY.md)、[系统分层架构规范](../../../docs/architecture.md)、[ADR-0008](../../../docs/adr/0008-analysis-first-v1-and-deterministic-key-level-engine.md)

**Status:** ready-for-agent

- [ ] 成交量位与锚定 VWAP 按截止时刻无前视计算，接入 10 的稳定身份、强度与状态机制，补黄金向量。
- [ ] 清算密集区算法的杠杆假设作为版本参数显式记录；类别标为 `ESTIMATED`，`get_key_levels` 可按观测 / 估算筛选。
- [ ] 看盘页对 `ESTIMATED` 使用不同的视觉样式与图例说明。
- [ ] 浏览器界面验收（均附截图）：宽度 `1440` 与 `390`、浅色与深色、简体中文下检查成交量位与清算估算区的差异化图例；覆盖持仓量数据不足时清算估算区为空的提示。
