# 07 — 永续衍生品数据（`Mark Price` / `Funding Settlement` / 持仓量）与衍生品指标

**What to build:** 交易员在看盘页看到当前 `Instrument` 的标记价格、实时资金费率、下次结算时间与持仓量，并可在副图打开资金费率分位数与持仓量背离度指标；这些数据进入 `Evidence Snapshot`，供关键位清算估算、预警与智能体读取。

**Blocked by:** 03 — 实时 K 线与盘口流、`NO_TRADES` / `STALE` 断流判定与顶栏健康提示；04 — 单一实现技术指标算子核与图表主副图指标

**Source of truth:** [需求文档](../spec.md)、[领域词汇表](../../../GLOSSARY.md)、[系统分层架构规范](../../../docs/architecture.md)、[ADR-0002](../../../docs/adr/0002-capability-traits-and-market-packs.md)、[ADR-0008](../../../docs/adr/0008-analysis-first-v1-and-deterministic-key-level-engine.md)

**Status:** ready-for-agent

- [ ] 连接器免密钥回填 `Mark Price` K 线、`Funding Settlement` 历史与 `5min+` 持仓量（游标翻页），并订阅实时 `Mark Price`、资金费率与持仓量；衍生品特征通过加密永续 `Market Pack` 挂载，核心层无市场分支。
- [ ] 上述数据带五重时点进入快照，`HISTORICAL_PIT` 截断正确。
- [ ] 在 04 的算子核中加入资金费率分位数、持仓量背离度与净赔率几何验算，遵守同一套等长 / 预热 / 断流语义并补充黄金向量。
- [ ] 快通道 WebSocket 推送衍生品实时值；看盘页顶栏或侧栏展示，副图可开衍生品指标。
- [ ] 浏览器界面验收（均附截图）：宽度 `1440` 与 `390`、浅色与深色、简体中文下检查衍生品状态区与衍生品副图；覆盖数值缺失（新上市标的无持仓量历史）时的空态。
