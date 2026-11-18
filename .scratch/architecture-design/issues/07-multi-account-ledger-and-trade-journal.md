# 设计多子账户复式账本、全成本归因与交易复盘日志模块

Labels: wayfinder:grilling
Type: grilling
Status: resolved
Blocked by: 04, 06

## Question

账本、绩效评测与复盘层如何支撑“多独立虚拟子账户 + 三类决策来源（`HUMAN / CO_PILOT / AGENT`）”的精确核算与深度复盘？具体需敲定：
1. 多子账户、多币种精确十进制复式账本的表结构与状态投影接口（现金、全仓/逐仓保证金、手续费、资金费率收支、在途冻结额度）；
2. 全成本归因与对照评测接口（拆解手续费、滑点、资金费率、决策延迟损耗及模型 Token 成本，支持跨子账户与按来源标签对比）；
3. `Trade Journal`（交易复盘日志）的存储与关联模型——如何将已平仓计划、开平仓时刻的图表 `Overlay` 快照、人工主观复盘笔记与对应 DeepSeek Harness `Trajectory` 思考轨迹永久关联以便回溯审阅。

## Answer

按推荐方案定型复式账本、全成本归因与复盘层（L5 `ledger_journal`）的核心表结构与公开接口契约：

### 1. 多子账户精确十进制不可变复式账本（`DoubleEntryLedger`）

1. **不可变分录模型与精度纪律**：
   - 采用仅追加（Append-only）复式分录双表结构：`ledger_entries`（记录 `entry_id`、`run_id`、`sub_account_id`、`event_id` 幂等键、`plan_id`、`DecisionOrigin`、`ts_event`、`entry_type`）与 `ledger_postings`（记录科目 `account_code`、币种 `currency`、借贷方向与定点十进制字符串金额 `amount_dec`）。每次入账事务强制断言同一分录内按币种借贷精确平衡（$\sum \text{debit} - \sum \text{credit} == \text{Decimal}('0')$），严禁直接 `UPDATE` 余额字段或混入 IEEE 754 浮点数。
2. **多市场科目细分与双轨持仓投影**：
   - 科目树按 `(run_id, sub_account_id, currency)` 严格物理隔离：`EQUITY_PAID_IN`（初始注资）、`CASH_FREE`（可用现金）、`CASH_FROZEN_PENDING`（在途限价开仓冻结资金/初始保证金）、`MARGIN_LOCKED_ISOLATED`（逐仓占用保证金）、`MARGIN_LOCKED_CROSS`（全仓占用保证金）、`REALIZED_GROSS_PNL`、`FEE_MAKER_TAKER`、`CARRY_FUNDING_NET`（永续资金费率净收付）、`CARRY_BORROW_FEE`（美股融券利息）、`LIQUIDATION_PENALTY`。
   - 持仓台账同步维护两种精确投影：① 衍生品合约净头寸与加权开仓均价视图（未实现盈亏与维持保证金率强制按最新 `Mark Price` 实时重估）；② 美股/现券 `FIFO Lot` 批次台账（按先进先出匹配闭合回合，并预留公司行动拆股/现金分红批次调整事件处理接口）。
3. **只读状态投影接口**：
   - `get_account_state(run_id: str, sub_account_id: str, mark_prices: dict[str, Decimal]) -> AccountState`：向 L3 组合风控层与前端工作台实时输出不可变账户快照（含净值、可用保证金、冻结额度、各标的持仓、滚动 5 交易日 PDT 日内回合计数与当日/峰值回撤率），是 L5 向 L3 唯一合法的只读反馈接口。

### 2. 六维全成本归因与双轴对照评测引擎（`CostAttributionEvaluator`）

1. **单笔闭合回合（`ClosedTradeRound`）六维瀑布拆解**：
   对每笔完成平仓/强平的计划，以决策快照时刻 $t_{\text{decision}}$ 的盘口中间价为无摩擦理论基准，精确拆解：
   $$\text{Net PnL} = \text{Theoretical Gross PnL} - \text{Latency Drift}(\Delta t) - \text{Execution Slippage} - \text{Exchange Fees} + \text{Net Carry (Funding/Borrow)} - \text{LLM Token Cost}$$
   其中 `Latency Drift` 度量从快照冻结 $t_{\text{decision}}$ 到越过延迟墙 $t_{\text{decision}} + \Delta t$ 期间的价格不利偏移损耗；`LLM Token Cost` 按该次洞察或会诊回合实际消耗的输入（区分缓存命中与未命中）/输出 Token 与模型单价折算为账户结算币种成本（纯代码量化与纯人工单该项为 `0`）。
2. **跨子账户 × 跨来源双轴对照接口**：
   - 支持按 `sub_account_id` 横向对比（例如同一行情期内 `智能体自治子账户` vs `双轨协同子账户` vs `纯人工子账户` vs `纯代码量化基线子账户` 的扣除全成本后净夏普、索提诺、盈亏比、胜率、换手率与最大回撤）。
   - 支持按 `DecisionOrigin`（`HUMAN | CO_PILOT | AGENT`）分组聚合，并针对 `CO_PILOT` 交易专门计算**人机协同净增值（`Co-Pilot Lift`）**：将智能体原始 `StagedPlanDraft` 在同一 `SimExecutionAdapter` 上静默影子跑出的虚拟净收益，与经人工修改入场/止损或主动干预平仓后的实际净收益做逐笔差分，量化评估“人工介入究竟是在帮智能体避坑增效，还是在反向微操损耗”。

### 3. `Trade Journal`（交易复盘日志）四维不可变关联模型

1. **自动建档与外键锚定**：
   - 每当一笔 `ApprovedExecutionPlan` 进入终态（`CLOSED | LIQUIDATED | CANCELLED`），系统自动生成一条 `TradeJournalRecord`，通过 `plan_id` 永久锚定四维上下文：
     - **① 执行与归因档案**：完整委托、部分成交、人工改止损（`BarrierAmendment`）时间轴与六维成本归因明细；
     - **② 开平仓盘面与图表画线冰封快照**：不仅绑定开仓 `entry_snapshot_id` 与平仓 `exit_snapshot_id`，还在计划提交与平仓瞬间将该标的当时可见的 `HUMAN_DRAWING`、`AGENT_ANNOTATION` 与 `EXECUTION_BARRIER` 序列化存为不可变 `frozen_overlays_json`——即便交易员数日后清空或改动了主图画线，在复盘面板点开历史交易时，`KLineChart` 仍能 100% 还原当时的支撑阻力线、智能体推演路径与进出场打点；
     - **③ DeepSeek Harness 认知轨迹溯源指针**：对 `AGENT` 与 `CO_PILOT` 来源交易，持久化 `dsh_profile`、`dsh_session_id`、`dsh_turn_id` 及 `$DSH_HOME` 下对应 Zstd JSONL `Trajectory` 文件路径与校验摘要（附原始 `StagedPlanDraft` 与人工修改后参数的 Diff），支持前端一键展开当时的完整工具调用、PTC 验算代码与思考链；
     - **④ 人工主观复盘批注与合规经验沉淀**：存储在 `workspace_state.sqlite` 的复盘扩展表中，支持录入执行纪律标签（如 `严格执行 / 情绪追单 / 止损过窄 / 提前止盈`）与复盘总结；仅当交易已完全平仓结算（具备确定的 `resolved_ts`）且经复盘确认后，才允许提炼结构化教训写入盘后经验索引（回测检索时硬性过滤 `resolved_ts <= cutoff_ts`，严防未来复盘记忆穿越污染历史回测）。


## Comments

### 交接前整体复查修订（覆盖已解决 5、8 张）
- **原因**：独立复查指出两项核算缺口：① 仅在已成交 `ClosedTradeRound` 扣减 Token 成本会漏计大量 `Abstain`、拒单、撤单、废弃草案与 `ControllerPatch` 的推理开销；② `Co-Pilot Lift` 反事实影子重跑需明确拒单、否决草案与资金隔离规则。
- **修订结论**：
  1. **子账户级全量认知成本台账**：账本新增 `EXPENSE_LLM_COGNITION` 科目与 `llm_invocation_costs` 流水表，每次 DSH 推理（含弃权、拒单、挂单超时、废弃草案、会诊与 `ControllerPatch`）实时入账；子账户级净收益与净夏普强制扣减同期全部认知推理总成本，彻底消除幸存者偏差。
  2. **`Co-Pilot Lift` 反事实沙箱隔离规则**：在草案生成时点基于独立无副作用 `CounterfactualSimSandbox`（快照当时子账户净值，不占用真实保证金与风险簇额度）对结构化 `StagedPlanDraft` 执行同构 L3→L4 模拟：原始草案被 L3 拒单则基准净利记 `0`；人工否决（`DISCARDED`）亏损草案记正避险增益；同子账户同向加仓合并后按完整仓位回合（`ClosedPositionRound`）统一对比，与复式账本已结盈亏恒等对齐。
