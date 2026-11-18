# 设计统一策略层接口与防过拟合参数寻优架构

Labels: wayfinder:grilling
Type: grilling
Status: resolved
Blocked by: 01, 03

## Question

策略层如何用清晰的统一抽象同时容纳“纯代码量化策略、DeepSeek Harness 智能体策略（战术洞察与参数调优双模式）及人工主观/协同交易计划”，并安全集成基于 `Optuna` 的回测参数寻优？具体需敲定：
1. 统一的策略输入输出接口契约、共享技术/衍生品确定性算子库（供纯代码策略、前端指标副图与 DSH PTC 模式三方复用同一口径）及市场领域 `Skill` 插件规范；
2. 参数回测寻优子模块（基于 `Optuna`）的架构边界：参数搜索空间声明方式、绑定 `snapshot_id` 的训练集/验证集/独立保留测试集物理隔离、寻优前置防前视（尾部截断对比）与指标预热漂移审计门禁，以及寻优结果晋升进入候选策略池的管理流程。

## Answer

按推荐方案定型策略与协同草案层（L2 `strategy`）及外圈参数寻优编排器（O1 `orchestration/study_optimizer`）接口契约：

### 1. 统一策略接口、三端同口径算子库与市场领域 `Skill` 规范

1. **统一策略输入输出契约与草案同构转换**：
   - 所有自动策略（纯代码量化策略与 DeepSeek Harness 智能体策略）实现同一无状态评估接口：`evaluate(snapshot: EvidenceSnapshot, triggers: list[TriggerEvent], active_patch: ControllerPatch | None) -> StrategyDecision`。
   - `StrategyDecision` 输出三种合法形态之一：① `list[SetupInsight]`（战术洞察模式：必填 `direction`、`entry_band`、`invalidation_price`、`take_profit_targets`、`trailing_rule`、`max_holding_ms`、`ttl_ms` 与非空 `evidence_refs`）；② `ControllerPatch`（快慢参数调优模式：仅允许更新目标量化控制器 `ParamSpace` 已声明白名单内的参数，越界直接拒绝）；③ 显式 `Abstain`（附弃权原因码如 `INSUFFICIENT_EVIDENCE`、`STALE_DATA`、`GEOMETRY_INVALID`、`MODEL_TIMEOUT`）。
   - 人工手动创建的计划与经 `StagedPlanDraft` 审阅确认的协同计划，由 `plan_drafter` 封装为与自动策略完全同构的 `PlanCandidate(insight, sub_account_id, origin, requested_qty_override)` 送入下游 L3 组合风控层，使 L3 对上游策略形态零分支。
2. **共享技术与衍生品确定性算子核（L0 `foundation/operators` + 黄金向量跨端对齐）**：
   - 将趋势/波动/动量指标（EMA、SMA、RSI、ATR、BOLL、Donchian、VWAP）及衍生品专属算子（资金费率年化与滚动分位数、OI 净变化率与价格背离度、标记-指数基差率、止损距离与净赔率验算）沉淀在 L0 纯函数模块中，无任何 I/O 或宿主依赖。
   - **统一序列规范**：输入长度为 $N$ 时输出严格为长度 $N$ 的**前导 `null` 等长索引对齐序列**，严禁丢弃头部预热元素导致多序列下标错位。
   - **三端口径一致性保证**：在 `contracts/operator_vectors/` 中固化标准输入输出黄金测试向量（精确到小数点后 8 位），同时约束 Python 核心算子、`dsh-strategy-agents` 暴露给 `@deepseek-ai/dsh-ptc-runtime`（PTC 模式）的算子工具以及 `talaria-desk-ui` 注册进 `KLineChart v10`（`registerIndicator`）的自定义副图指标，确保“人眼看图、模型 PTC 验算、回测撮合”三端数值严格一致；禁止智能体心算任何多步浮点指标。
3. **DeepSeek Harness 智能体策略与市场 `Skill` 插件规范**：
   - 按 `Market Pack` 在 `dsh-strategy-agents/skills/<market>/SKILL.md` 注册领域分析纪律（如 `crypto-perp` 聚焦资金费率拥挤度、OI 清算挤压与 `Mark Price` 基差；`us-equity` 聚焦盘前跳空缺口、常规/盘后流动性断层与财报 `8-K` 催化）。
   - 提示词固定按“① 全局风控与输出契约静态前缀（`AGENTS.md`）→ ② 当前标的匹配的市场 `Skill` → ③ 当前冻结 `EvidenceSnapshot` 增量载荷”三段式拼装，最大化 DeepSeek 模型前缀缓存命中率。凡模型输出缺止损价、多空止损止盈方向颠倒、引用快照外证据或超时，适配器一律强制降级为 `Abstain`。

### 2. 基于 `Optuna`（MIT）的防过拟合参数寻优架构（位于外圈 O1，严禁引入 GPL-3.0 源码）

1. **声明式有界参数空间（`ParamSpace`）**：
   - 策略通过强类型描述符（`IntParam`、`DecimalParam`、`CategoricalParam`、`BoolParam`）声明默认参数、搜索上下界、步长及参数间关系断言（如 `short_window < long_window`），单次寻优默认软硬限制可调参数维度（推荐 `<= 6` 维），从源头抑制高维曲线拟合。
2. **寻优启动前强制双审计门禁（未通过直接拒绝启动 `study.optimize()`）**：
   - **截断式前视偏差审计（`LookaheadAudit`）**：洁净室自研尾部截断对比测试——在训练区间内抽取 $K$ 个截断时点 $t_k$，分别用截断快照 `cutoff = t_k` 与完整快照 `cutoff = T_train_end` 运行被测策略，断言所有 `<= t_k` 时刻产出的 `SetupInsight` / 指标值逐字节相等，揪出未来函数或全区间归一化泄漏。
   - **递归指标预热漂移审计（`WarmupDriftAudit`）**：对比 `1x warmup_bars` 与 `2x warmup_bars` 预热窗口下正式评测起点处的 EMA/RSI/ATR 数值与信号差异，超出容差（如 `1e-6`）则阻断并提示调大预热长度。
3. **绑定 `snapshot_id` 的三段式物理隔离与全试验留痕晋升流程**：
   - **物理封存切分**：创建 `OptunaStudy` 时强制绑定不可变冷库 `dataset_snapshot_id`，并将时间轴切分为 `Train`（拟合窗）、`Validation`（选参与 Walk-Forward 滚动验证窗，窗口交界处强制插入 `>= max_holding_ms + warmup` 的 `embargo` 静默清洗带）与 `Holdout OOS`（独立封存盲测窗）。在采样迭代期传入 `ReplayRunner` 的快照读取器在接口层物理裁掉 `Holdout OOS` 时间段。
   - **采样、约束与多进程无锁存储**：基于 `optuna v5.1`（MIT），单/双目标默认采用 `TPESampler`（3+ 目标可选 `NSGAIIISampler`，并显式禁用多目标下不支持的 `trial.report()` 中间剪枝），通过 `trial.set_constraint` 施加最低有效交易笔数与最大回撤硬约束；底层采用 `JournalStorage`（`JournalFileBackend` 追加日志）支撑多 Worker 进程并行回测，并在 `study_registry.sqlite` 中不可篡改地累计记录全部 `n_trials`（含失败与不达标试验）。
   - **邻域稳健性检验与候选晋升门禁**：从帕累托前沿选出的参数必须先通过**参数邻域高原检验**（对各连续参数施加 `±5% / ±10%` 网格微扰，邻域中位净收益不得断崖式跌破零或偏离峰值超阈值），随后仅允许在封存 `Holdout OOS` 上执行**单次终审盲测**，连同累计试验次数统计折扣分（如 Deflated Sharpe Ratio）写入策略注册表 `CANDIDATE` 状态，经人工在复盘台审阅签字后方可挂载至 `Shadow Run` 子账户；一旦查看 `Holdout OOS` 后继续改参重跑，系统自动将该段标记为 `TAINTED_VALIDATION` 要求更换新盲测期。


## Comments

### 交接前整体复查修订（覆盖已解决 5、8 张）
- **原因**：独立复查指出三处缺口：① 序列中间出现休市/无成交/断流时三端递归算子与 `KLineChart` `dataList` 下标对齐规则未定；② `ControllerPatch` 缺少持久化过期与归因链路；③ `ALPACA_SIP` 历史寻优向 `ALPACA_IEX` 实时源晋升存在成交量量纲失真。
- **修订结论**：
  1. **双层序列契约与断流重预热**：区分横截面 `GridAlignedTable` 与单标的紧凑交易序列 `SessionCompactBars`（剔除正常 `SESSION_CLOSED` 空槽，与 `KLineChart` `dataList` 严格 1:1 等长对齐）。单标的算子定义在紧凑序列上：`NO_TRADES / HALTED` 按前收盘平盘零量柱保态递推；遇真实异常 `FEED_GAP` 立即置 `null` 并强制重跑完整 `warmup_bars` 预热期，纳入 `contracts/operator_vectors/` 黄金回归测试。
  2. **`ControllerPatch` 生命周期与溯源**：补丁持久化于 `collaboration_state.sqlite`（含 `patch_id`、`valid_until_ts`、`dsh_turn_id`），到期未续签自动回退默认参数；生效期内快层策略触发的计划记 `cognition_mode = CONTROLLER_PATCH` 并关联 `patch_id` 溯源至对应 DSH `Trajectory`。
  3. **跨 `feed_id` 晋升硬约束**：基于 `ALPACA_SIP` 寻优的策略若挂载至 `ALPACA_IEX` 实时子账户，禁止包含绝对成交量阈值参数，且须先通过 `ALPACA_IEX` 同源历史切片一致性核验。
