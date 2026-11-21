# 05 — 盘口加逐笔模拟成交插件

**What to build:** 交易方案选 `fill: l1_trade_sim` 后，逐笔与报价分别判断覆盖和时效：有逐笔的区间按成交顺序判定止损与止盈先后；吃单价有新鲜报价时按买一卖一，只有逐笔或报价过期时按逐笔价加配置点差；两者都没有时退回 1m K 线模拟。每笔成交标注所用的降级档位。

**Blocked by:** data-access/07 — 逐笔成交与买一卖一落盘；03 — 默认成交插件；backtest-and-research/01 — 回放编排。

**Source of truth:** `docs/architecture.md` §6.4；`docs/research/fill-simulation-and-tick-data.md`。

**Status:** ready-for-agent

- [ ] 与 03 共用 `PositionExecutor` 与屏障逻辑，只替换撮合判定。
- [ ] 读取逐笔与报价时带 `as_of`，不看到决策时刻之后才可见的数据。
- [ ] 测试：同根 K 线内先触及止盈的逐笔路径按止盈结算；只有逐笔、报价过期、两者都缺三种输入分别按对应档位成交并标注。
- [ ] 同一方案在两种成交插件下的结果差异写入 backtest-and-research/01 的回放报告。
