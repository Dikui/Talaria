# 05 — 盘口加逐笔模拟成交插件

**What to build:** 交易方案选 `fill: l1_trade_sim` 后，逐笔与报价分别判断覆盖和时效：有逐笔的区间按成交顺序判定止损与止盈先后；吃单价有新鲜报价时按买一卖一，只有逐笔或报价过期时按逐笔价加配置点差；两者都没有时退回 1m K 线模拟。每笔成交标注所用的降级档位。

**Blocked by:** data-access/07 — 逐笔成交与买一卖一落盘；03 — 默认成交插件；backtest-and-research/01 — 回放编排。

**Source of truth:** `docs/architecture.md` §6.4；`docs/research/fill-simulation-and-tick-data.md`。

**Status:** ready-for-agent

- [ ] 与 03 共用 `PositionExecutor` 与屏障逻辑，只替换撮合判定。
- [ ] 读取逐笔与报价时带 `as_of`，不看到决策时刻之后才可见的数据。
- [ ] 测试：同根 K 线内先触及止盈的逐笔路径按止盈结算；只有逐笔、报价过期、两者都缺三种输入分别按对应档位成交并标注。
- [ ] 同一方案在两种成交插件下的结果差异写入 backtest-and-research/01 的回放报告。

## Comments

### 实现记录

- 注册 `fill: l1_trade_sim`。两个成交插件共用 `PositionExecutor`、保护屏障、决策延迟、执行风控和复式记账。逐笔路径按事件时间及冷库同毫秒顺位推进。在同一毫秒的先后不可判定时，若已触及止损则优先执行止损。部分止损的余量在行情降级后继续按止损出场。
- 撮合窗口的逐笔读取以窗口边界和回放时钟限制 `as_of`。报价在具体成交时刻再按可获取时间、连接内更新序号和有效期取最新值。默认有效期为 `quote_max_age_ms: 3000`。主动成交用新鲜的买一或卖一并叠加冲击滑点。逐笔档按逐笔价加配置的半点差与滑点。缺逐笔、有可用 1m 柱时沿用保守的柱内路径，撮合时仍优先使用当时新鲜报价。没有可用柱时，逐笔和报价可独立驱动撮合。
- 成交事件持久化 `fill_tier`，分别为 `l1`、`trade`、`bar_1m`。标记价继续触发强平。强平价格依次取可见且有效的盘口、近一分钟内可见的逐笔、加摩擦的标记价。最后一种记为 `mark`。
- `talaria replay` 发现 `l1_trade_sim` 时，会用同一生效方案、区间、策略代码哈希及冷库快照多跑一份 `bar_sim_1m`。报告通过 `fill_plugin` 标识两次运行，在 `fill_comparisons` 中按「逐笔盘口插件减 1m 插件」列出交易笔数和各币种的入账指标差值。逐笔和报价清单在原有 SQLite 读事务中一起固定。回放用与 SDK 共用的冷库读取器，逐笔按 5 分钟页读入并逐窗口做时点选版。
- 按架构 §6.4 的逐笔规则解释 ADR-0003 的同柱止损优先口径。任务单没有删除类验收项。真实模拟盘接入、生产规模长区间吞吐和内存峰值仍待验证。成交 ID 跨文件选源和选版会额外扫描范围外的 ID 列。

#### 验证

- red → green 覆盖同毫秒顺位、时点与报价降级、缺柱成交、强平成交价、部分止损跨档延续以及回放报告的固定清单和差值。
- 按任务说明的解释器与 `PYTHONPATH` 运行 `-m pytest tests/account tests/orchestration tests/perception/test_tick_storage.py tests/perception/test_cold_store.py tests/sdk tests/packs/test_margin_valuation.py tests/foundation/test_import_boundaries.py tests/cli/test_contract_check.py -q -p no:cacheprovider --tb=short -rs`，最终 365 项通过、0 项失败、5 项 Seatbelt 用例因沙箱跳过，无 TCP 或网络失败。
- `contract-check` 零漂移。`git diff --check` 通过。全量测试未运行。
- 显式逐路径暂存返回 128，沙箱拒绝在主仓库的 worktree Git 元数据目录创建 `index.lock`。工作区保留完整改动。分支 HEAD 为 `138451e42bd963fa8a689256c676979479b02001`，拟提交信息为 `feat: 实现盘口加逐笔模拟成交`。
