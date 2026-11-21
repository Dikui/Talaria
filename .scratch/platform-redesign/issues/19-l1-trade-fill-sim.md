# 19 — 盘口加逐笔模拟成交插件

**What to build:** 交易方案选 `fill: l1_trade_sim` 后，有逐笔数据的区间按成交顺序判定止损与止盈先后、按买一卖一计算吃单价；没有逐笔的区间退回 1m K 线模拟并在结果中标注。

**Blocked by:** 07 — 逐笔成交与买一卖一落盘；16 — 默认成交插件。

**Source of truth:** `docs/architecture.md` §6.4；`docs/research/fill-simulation-and-tick-data.md`。

**Status:** ready-for-agent

- [ ] 与 16 共用 `PositionExecutor` 与屏障逻辑，只替换撮合判定。
- [ ] 测试：同根 K 线内先触及止盈的逐笔路径按止盈结算；数据缺口时回退并标注。
- [ ] 同一方案在两种成交插件下的结果差异写入回放报告。
