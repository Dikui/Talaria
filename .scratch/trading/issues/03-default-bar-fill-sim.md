# 03 — 默认成交插件：1m K 线模拟成交与四重保护屏障

**What to build:** 交易方案选 `fill: bar_sim_1m` 后，获批计划由 `PositionExecutor` 托管回合全过程，按六条诚实仿真法则成交，止损、止盈、移动止损、最长持仓时间在本地自治触发，资金费率结算与标记价强平按冷库的衍生品数据执行，事件写入账本。

**Blocked by:** 02 — 默认定仓与风控插件（共用 `MarginValuation`）；data-access/08 — 衍生品进入冷库与实时订阅。

**Source of truth:** `docs/architecture.md` §6.4；ADR-0003；GLOSSARY 的 Exit Barrier Set、Position Executor、Decision Latency。

**Status:** ready-for-agent

- [ ] 实现 `ExecutionEngine` 六个方法；只减风险的方法内部断言风险敞口单调不增。
- [ ] 六条法则各有测试：延迟墙、挂吃单、同根 K 线先止损、跳空滑点、`Mark Price` 强平与资金费率结算（无服务回测，数据来自 data-access/08 的冷库）、规范化事件与分录可复现。
- [ ] 回合状态流转覆盖部分成交、同向加仓合并、撤单、强平。
