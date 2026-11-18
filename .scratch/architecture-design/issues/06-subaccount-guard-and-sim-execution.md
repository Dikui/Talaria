# 设计多子账户组合风控层与带实盘预留位的模拟执行层

Labels: wayfinder:grilling
Type: grilling
Status: resolved
Blocked by: 01, 03

## Question

在支持多个资金隔离的虚拟子账户前提下，组合与风控层及执行层的深模块接口与内部状态机应如何定型？具体需敲定：
1. 组合与风控层接口：单子账户内的 `Risk Unit` 止损距离反推定仓、跨品种相关性簇敞口上限、衍生品维持保证金强平安全垫、美股分段交易时段/PDT 规则以及净赔率门槛的确定性校验流水线；
2. 执行层的四方法替换接口（`Execution Seam`）、单笔计划四重保护屏障（硬止损、止盈、激活式移动止损、持仓时间上限）状态机（含盘中人工拖动图表止损线联动修改活跃屏障的协议）、高保真模拟撮合物理规则（决策延迟墙 `Δt`、挂吃单滑点、同根 K 线悲观止损、永续整点 `Funding Settlement`）及未来实盘网关适配器的预留规范。

## Answer

按推荐方案定型多子账户组合与风控层（L3 `portfolio_risk`）及执行与高保真仿真层（L4 `execution`）的深模块接口与状态机：

### 1. 多独立虚拟子账户组合定仓与硬风控七步流水线（L3 `portfolio_risk`）

1. **虚拟子账户（`SubAccountSpec`）隔离边界**：
   - 每个虚拟子账户拥有独立的初始本金、计价结算币种（如 `USDT` / `USD`）、准入市场范围、保证金制度（永续 `CROSS | ISOLATED`，美股 `CASH | REG_T`）、风险参数（单笔标准 `r_unit_pct`、单笔硬上限 `max_single_r_pct`、相关性簇上限 `cluster_max_r`、日内与峰值最大回撤熔断线）及独立的 `TradingState`（`ACTIVE | REDUCING | HALTED`）。不同子账户（如纯人工盘、双轨协同盘、智能体自治盘、纯量化基线盘）资金、保证金强平与风控熔断 100% 物理隔离。
2. **公开深接口（同步支持正式审批与前端毫秒级无副作用预试算）**：
   - `evaluate_candidate(candidate: PlanCandidate, account_state: AccountState, snapshot: EvidenceSnapshot) -> ApprovedExecutionPlan | RejectedPlanRecord`
   - `preview_sizing(draft: StagedPlanDraft, account_state: AccountState, snapshot: EvidenceSnapshot) -> SizingPreview`（纯函数只读试算，供前端拖动止损/入场线时实时展示仓位、强平缓冲与净赔率）。
3. **七步确定性硬风控校验顺序**：
   - **Step 1（账户状态与止损几何校验）**：校验目标子账户 `TradingState == ACTIVE`；校验 `invalidation_price` 非空且与方向严格自洽（做多 `stop < entry_band.low`，做空 `stop > entry_band.high`）。
   - **Step 2（`Risk Unit (R)` 止损距离反推定仓）**：按最不利入场边界价 $P_{\text{entry,worst}}$ 与双边摩擦缓冲 $c_{\text{buf}}$ 计算理论数量 $Q_{\text{raw}} = \frac{E_{\text{sub}} \cdot r_{\text{unit}}}{(|P_{\text{entry,worst}} - P_{\text{stop}}| + P_{\text{entry,worst}} \cdot c_{\text{buf}}) \cdot M}$，按 `lot_size` 向下取整并校验 `>= min_qty / min_notional`；若 `HUMAN / CO_PILOT` 传入手动指定数量，则反推其隐含亏损额，超出 `max_single_r_pct` 直接拒单。
   - **Step 3（跨标的同向相关性簇风险预算）**：汇总该子账户内在途挂单及活跃持仓当前的未锁定风险敞口 $\sum R_{\text{open}}$（已通过移动止损推过保本成本线的仓位其剩余下行风险按 `0` 计），叠加本笔新增 $R$ 不得突破所属风险簇上限（如加密主流币多头簇、美股高 Beta 科技簇）。
   - **Step 4（`SessionTrait` + `OrderConstraintTrait` + 催化剂静默门禁）**：检查当前 `Trading Session` 允许的委托标志（如美股盘前/盘后禁发市价单、检查美股 `< $25,000` Reg-T 子账户滚动 5 交易日内剩余 PDT 日内开平次数），并按子账户配置拦截重大宏观/财报事件公布前 $X$ 分钟内的新开仓。
   - **Step 5（`MarginTrait` 保证金占用与强平安全垫硬约束）**：基于当前 `Mark Price` 试算成交后子账户初始保证金充足率及预估维持保证金强平价 $P_{\text{liq}}$，强制要求硬止损价显著先于强平价触发（留足配置的安全缓冲比例与最小跳数），杜绝高杠杆下未及止损先遭强平。
   - **Step 6（`CarryTrait` 扣除全摩擦净赔率门槛）**：扣除预估双边手续费、盘口冲击滑点及预计持仓上限期内应付资金费率/融券息后，净收益风险比低于子账户最低门槛则拒单（附明确 `RejectionCode`）。
   - **Step 7（签发 `ApprovedExecutionPlan`）**：冻结 `plan_id`、`sub_account_id`、`DecisionOrigin`、入场委托参数、`TripleBarrierSpec` 与决策基准时刻 `ts_decision`。

### 2. 执行层深接口（`Execution Seam`）、四重屏障状态机与诚实仿真物理法则（L4 `execution`）

1. **五方法深接口 `ExecutionEngine`（扩展原四方法以原生闭环人工拖线改屏障与紧急减平仓）**：
   - `submit_plan(plan: ApprovedExecutionPlan) -> PlanReceipt`：唯一合法新开/加仓入口；
   - `amend_barrier(req: BarrierAmendmentRequest) -> BarrierAmendmentReceipt`：处理人工在 `KLineChart` 拖动活跃仓位止损/止盈线或点击“一键保本”。**内置安全不变量**：`REDUCING / HALTED` 状态下仅允许向收紧风险方向移动止损；`ACTIVE` 状态下若放宽止损，必须回调 L3 纯函数校验新止损位仍满足单笔最大风险上限与衍生品强平安全垫，否则原位弹回并明确报错；
   - `intervene_position(plan_id: str, action: Literal["CANCEL_ENTRY", "CLOSE_ALL", "REDUCE_RATIO"], ratio: Decimal | None, reason: str) -> PlanStatus`：人工紧急撤单/市价全平/按比例减仓毫秒级直通入口；
   - `set_trading_state(sub_account_id: str | None, state: TradingState, reason: str) -> None`：支持单子账户或全局切换 `ACTIVE / REDUCING / HALTED`（切入 `HALTED` 时自动撤销该范围所有未成交开仓挂单，保留已有仓位四重屏障继续护盘）；
   - `poll_events(up_to_ts: int) -> list[ExecutionEvent]`：按时间单调输出标准执行事件流。
2. **`PositionExecutor` 四重保护屏障本地自治状态机**：
   - 状态迁移：`PENDING_ENTRY`（监控入场成交与 `ttl_ms` 挂单超时撤单）→ `PARTIALLY_FILLED_PROTECTED`（首笔部分成交瞬间立即按已成交量激活四重屏障；若剩余挂单超时取消，已成交部分平滑转入全保护态）→ `ACTIVE_PROTECTED`（本地毫秒/Tick/K线级独立盯守：**① 硬止损下界、② 止盈目标上界、③ 达到激活阈值后只升不降/只降不升的移动止损、④ `max_holding_ms` 最长持仓垂直时间墙**）→ `EXITING` → `CLOSED | CANCELLED | LIQUIDATED`。全程不依赖大模型或工作台在线。
3. **`SimExecutionAdapter` 六条诚实仿真物理法则（历史回放与实时 `Shadow Run` 同构复用）**：
   - ① **决策延迟墙（`Decision Latency Δt`）**：快照冻结于 $t_{\text{decision}}$ 的计划，仅允许在 $t \ge t_{\text{decision}} + \Delta t$ 之后到达的盘口/K线上判定成交（回测注入实测模型耗时或保守延迟分布，`Shadow Run` 取真实墙钟耗时）；
   - ② **挂吃单微观撮合区分**：`POST_ONLY` 若到达瞬间穿过对手最优价则按场所规则拒单；普通 `LIMIT` 要求后续成交价/盘口实质穿过限价方可按 Maker 费率成交；市价或穿价限价单按对手盘价格叠加波动率/点差滑点模型并计 Taker 费率；
   - ③ **同根 K 线悲观止损优先**：在仅有分钟/秒级 OHLCV 而无逐笔路径时，若同一根 K 线高低点同时触及硬止损与止盈，一律按**先触发止损**保守结算；
   - ④ **跳空穿价真实滑点**：跨时段开盘跳空或剧烈插针越过止损价时，按跳空后真实可成交价格（而非理想止损触发价）撮合结算；
   - ⑤ **永续 `Mark Price` 强平与周期持仓现金流结算**：加密永续严格按 `Mark Price`（而非最新成交价）实时核查子账户维持保证金率，跌破时生成 `PositionLiquidated` 事件并计入强平清算费；持仓跨越结算整点自动按 $-\text{signed\_qty} \times M \times P_{\text{mark}} \times R_{\text{funding}}$ 产出 `FundingSettled` 事件；美股融券空头跨日自动产出 `BorrowFeeAccrued` 事件；
   - ⑥ **确定性字节级可复现**：相同输入快照序列、摩擦参数与随机种子下，输出 `ExecutionEvent[]` 严格一致。
4. **`LiveExecutionAdapter` 架构预留卡槽**：
   - 当前仅保留满足 `ExecutionEngine` 契约的桩模块与接口测试；日后接入真实交易所/券商时，发送前本地持久化 Outbox、幂等 `client_order_id`、网络超时 `UNKNOWN` 状态主动查询对账及原生场所 API 适配全部封装在该 Adapter 内部，上游 L1–L3 与下游 L5 无需修改任何接口。


## Comments

### 交接前整体复查修订（覆盖已解决 5、8 张）
- **原因**：独立复查指出四项核心机制需闭环：① 同子账户同标的多独立 `plan_id` 加仓或反向开仓会与净头寸/FIFO 账本产生平仓成本基准错位及双向冲销孤儿屏障；② 放宽止损不能由 L4 反向调 L3；③ 全仓强平需合法读取账户权益且最大回撤熔断缺盘中守护；④ `ALPACA_IEX` 在 `17:00–20:00 ET` 无实时报价盯止损。
- **修订结论**：
  1. **单子账户单标的单一主仓位回合不变量**：同一 `(sub_account_id, instrument_id)` 同一时刻最多允许一个方向的活跃仓位回合（多策略并行通过多独立虚拟子账户隔离）。同向获批加仓合并注入当前活跃 `PositionExecutor` 统一管理屏障并归入同一闭合回合核算；反向开仓草案在 L3 默认拒单（`OPPOSITE_POSITION_OPEN`），须先平清当前回合再反手。
  2. **接口拆分保持严格单向**：L4 将 `amend_barrier` 拆为直通收紧接口 `tighten_barrier`（仅允许缩窄止损距离）与受控应用接口 `apply_approved_amendment`（仅接收经 L3 校验签发的放宽凭证）。
  3. **全仓估值输入与回撤熔断守护**：`SimExecutionAdapter` 通过步进参数接收由 O1 投喂的只读 `MarginValuationView` 精确计算 `CROSS` 维持保证金强平；L3 Step 1 增加回撤线前置门禁，同时由 O1 `DrawdownCircuitBreaker` 实时监控净值回撤——触及软阈值切 `REDUCING`，触及硬熔断切 `HALTED` 并按子账户 `liquidate_on_halt` 配置自动清仓。
  4. **数据源供流窗口与持仓时段联动**：绑定 `ALPACA_IEX` 免费实时源的子账户由 L3 与 `SessionTrait` 强制限制在有实时报价覆盖的窗口内开仓/日内持仓（默认不晚于 `16:55 ET` 收盘平仓，禁在 `17:00–20:00 ET` 无源窗口裸持日内仓）。
