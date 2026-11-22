# 03 — 默认成交插件：1m K 线模拟成交与四重保护屏障

**What to build:** 交易方案选 `fill: bar_sim_1m` 后，获批计划由 `PositionExecutor` 托管回合全过程，按六条诚实仿真法则成交，止损、止盈、移动止损、最长持仓时间在本地自治触发，资金费率结算与标记价强平按冷库的衍生品数据执行，事件写入账本。

**Blocked by:** 02 — 默认定仓与风控插件（共用 `MarginValuation`）；data-access/08 — 衍生品进入冷库与实时订阅。

**Source of truth:** `docs/architecture.md` §6.4；ADR-0003；GLOSSARY 的 Exit Barrier Set、Position Executor、Decision Latency。

**Status:** ready-for-agent

- [ ] 实现 `ExecutionEngine` 六个方法；只减风险的方法内部断言风险敞口单调不增。
- [ ] 六条法则各有测试：延迟墙、挂吃单、同根 K 线先止损、跳空滑点、`Mark Price` 强平与资金费率结算（无服务回测，数据来自 data-access/08 的冷库）、规范化事件与分录可复现。
- [ ] 回合状态流转覆盖部分成交、同向加仓合并、撤单、强平。

## Comments

### 实现记录

- 注册 `fill: bar_sim_1m` 并实现成交插槽的六个执行方法。每个 `PositionExecutor` 管理入场余量、同向加仓、四重保护屏障和终态。收紧止损、撤单、减平仓及切换交易状态均断言持仓量、挂单量和止损风险单调不增。
- 仅用已确认且有成交量的 1m 柱撮合。入场柱开盘须越过决策延迟墙。限价挂单须严格穿价，主动成交包含半点差及冲击滑点，并按标的价格步长向不利方向取整。部分入场共用逐柱成交量上限。默认延迟 1ms，点差及 Maker 费率均为 2bp，入场成交量上限为该柱成交量的 10%。Taker 费率及冲击滑点沿用子账户配置。强平罚金可配置，默认为零。
- 同柱冲突优先止损。跳空止损按柱开盘加摩擦成交。多个止盈价以最先可能成交的目标全平。盘中挂单入场后，以收盘价确认同柱的有利走势。移动止损在该柱确认后生效。最长持仓时间从首笔成交起算，缺少可成交行情时进入 `EXITING`，等待下一可执行的价格。
- Parquet 入口把 1m 撮合行情、策略周期的确认柱、标记价及实际结算按可获取时点推进。回放直接读取与 SDK 同一冷库的修订，以便依次处理当时可见的版本。结算迟到时，根据结算点的持仓量及可见标记价计算，在确认时刻入账；费率修订补差额。缺少有效标记价的结算记 `FUNDING_UNPRICED`。强平共用 `MarginValuation` v2，写明 `LIQUIDATED` 终态并结清持仓成本、保证金、毛利及费用。账户宿主逐笔入账并检查熔断。调整止损前先入账已发生的减仓和屏障变更。
- 本任务未发现与指定文档的直接冲突。§6.4 中美股空头跨日融券费依赖未来的美股市场包与融券费率数据。本次实现覆盖现有永续市场包。任务单无删除类验收项。

#### 验证

所有 pytest 命令使用 `PYTHONPATH=$PWD/core/src /Users/dikui/Project/Talaria/.venv/bin/python -m pytest <路径> -q -p no:cacheprovider --tb=short`。以下每对数字为通过数 / 失败数，按各命令的执行顺序列出。

| pytest 路径 | 结果 |
| --- | --- |
| `tests/account/test_bar_sim_1m.py` | 0/6、3/3、6/0、6/8、14/0、14/1、14/1、15/2、17/0、18/1、19/0、20/1、21/1 |
| `tests/account tests/packs/test_margin_valuation.py tests/sdk/test_derivatives.py tests/foundation/test_import_boundaries.py tests/cli/test_contract_check.py` | 194/1、201/0、202/0 |

首轮红测及中间失败对应实现缺失、测试价格或费用预期修正、方案配置中的十进制字符串，以及迟到行情和屏障变更的回归用例。最后一轮有一条既有 Starlette 弃用警告。两次纯离线回放的规范化事件与复式分录一致。 `contract-check` 零漂移。已自查 `git diff main...HEAD`、工作区 diff 和新增文件，`git diff --check` 通过。

#### 未验证项与风险

- 未运行全量测试。定向回归没有因 TCP 或网络沙箱失败的用例。真实实时订阅、模拟盘、长期大区间回放的内存及耗时未验证。1m OHLC 无逐笔路径，同柱时序按上述保守规则处理。
- 逐路径暂存返回 128。沙箱禁止在主仓库的 worktree Git 元数据目录创建 `index.lock`。改动留在 `ticket/trading-03` 工作区，HEAD 为 `017eec5de8611465c5d15addd194beb9bbc700dc`。拟提交信息为 `feat: 实现默认 1m K 线模拟成交`。
