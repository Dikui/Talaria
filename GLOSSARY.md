# Talaria

面向多资产日内与波段交易的智能体研究与交易系统上下文：在严格时点数据纪律下融合智能体认知推理与确定性资金风险控制，首发覆盖加密永续合约与美股，并支持跨市场扩展。

## 产品形态与交易方案

**Resident Service（常驻服务）**:
由 launchd 守护、独立于任何窗口运行的本机后台进程，是行情、冷库、快照与协同状态的唯一写者，其余页面、Notebook 和账户进程都是它的使用方。
_Avoid_: Daemon, backend, server

**Account Process（账户进程）**:
承载一个或一组虚拟子账户、按交易方案装配插件并运行回测或模拟盘的独立进程；模拟盘时持有这些子账户的排他锁，是其账本的唯一写者。
_Avoid_: Strategy process, bot, worker

**Trading Scheme（交易方案）**:
为账户进程的每个插槽选定插件及参数的一份配置；不同交易方式只换方案，不改框架。
_Avoid_: Strategy config, setup, profile

**Plugin Slot（插槽）**:
账户进程中可替换的一个环节（数据入口、策略、定仓、风控、成交、工具），插件按带版本号的接口填入。
_Avoid_: Hook, extension point, layer

**Component（组件）**:
指标、信号或策略三类可复用计算单元的统称，带输入、参数范围、输出类型与版本元数据，上一类可以引用下一类。
_Avoid_: Script, study, module

**Runtime Context（运行时上下文 `ctx`）**:
组件取数的唯一入口，按当前时刻截断、只暴露已收盘的柱和已收盘的大周期柱，使防前视不依赖组件写法。
_Avoid_: Data handle, environment

**Rule（规则）**:
只调用已注册算子、带版本号的 JSON 表达式树写法的组件，可以安全地在常驻服务中求值，用于图表规则、预警与扫描；分收盘求值与事件驱动两种求值时机。
_Avoid_: Script, Pine code, formula

## 数据与时点语义

**Evidence Snapshot（证据快照）**:
在指定决策截止时刻冻结的、当时真实可获取的市场行情、衍生品状态指标与事件文本的不可变数据集合。
_Avoid_: Data cache, latest view, market dump

**Event Time（事件时间）**:
某笔市场成交、K 线闭合或财务报告期在客观世界实际发生或截止的时刻。
_Avoid_: Timestamp, record time

**Available Time（可获取时间）**:
某条信息的具体修订版本首次通过指定外部渠道可被市场参与者合法读取的时刻及其精度证明。
_Avoid_: Publish date, report period

**Committed Time（入库可见时间）**:
本系统实际完成某批次数据的接收、校验并向内部模块正式公开的时刻，用于系统实际观察回放。
_Avoid_: Fetch time, update time

**Revision（数据修订）**:
同一业务事实在不同时间发布的独立内容版本，新修订追加保留而不覆盖旧版本。
_Avoid_: Overwrite, hotfix row

## 标的、看盘与事件感知

**Instrument（交易标的）**:
在特定交易场所挂牌、具有唯一身份及精确价格与数量步长规格的可交易金融合约或证券。
_Avoid_: Ticker, coin, stock symbol

**Market Pack（市场规则包）**:
封装某一特定市场与资产类别的交易时段、保证金计量、持仓成本结算、指令约束及领域分析技能的可插拔扩展单元。
_Avoid_: Exchange script, market branch

**Watchlist（自选观察池）**:
由交易员手工维护或由扫描器动态归集的重点跟踪标的分组集合。
_Avoid_: Favorites, ticker list

**Alert（盘面预警）**:
预设在交易标的上的价格、量能、技术指标或资金费率阈值条件，触发时同时面向人工工作台提示与智能体上下文注入。
_Avoid_: Alarm, push message

**Catalyst Calendar（事件日历）**:
带有预期发生时刻与实际公布时刻的宏观数据发布、公司财报披露及重大市场事件时间表。
_Avoid_: News stream, cron schedule

**Chart Overlay（图表画线标注）**:
附着在特定交易标的时间-价格坐标系上的结构化几何图元（如支撑阻力线、趋势通道、区间标注），严格区分人工手绘标注与经价格范围校验的智能体建议标注。
_Avoid_: Canvas shape, raw drawing JSON

**Key Level（关键位）**:
由确定性算法仅基于截止时刻前已可见数据识别出的重要价格或价格区间（如确认后的摆动高低点、时段参考位、成交量分布价位、缺口与整数关口），具有跨快照稳定的身份、强度评分与有效状态，人与智能体共享引用。
_Avoid_: AI level, guessed support, drawn line

**Mark Price（标记价格）**:
衍生品场所综合现货指数与基差计算的公允参考价格，专门用于核算未实现盈亏与判定维持保证金强平，防止单所异常插针误触发强平。
_Avoid_: Last trade price, mid price

**Funding Settlement（资金费率结算）**:
永续合约在固定结算时点由多空持仓方之间按标记价格名义价值与当期资金费率直接收付的周期性持仓现金流。
_Avoid_: Interest fee, commission

**Trading Session（交易时段）**:
标的允许接收特定订单类型并撮合成交的时间窗口定义（如加密市场连续时段，或美股盘前、常规、盘后与夜盘分段时段）。
_Avoid_: Business hours

## 双轨认知、触发与信号

**Co-Pilot Analysis（人机协同分析）**:
交易员与智能体共享同一套时点数据、图表关键位标注与风险视图，既可各自独立发起分析，也可互相审阅、补充和修正交易计划的协作机制。
_Avoid_: Chatbot Q&A, manual override

**Trigger（唤醒触发事件）**:
由确定性数值扫描器或盘面预警在标的上捕获到的结构性异动事件，是触发智能体或提醒人工介入分析的前置信号。
_Avoid_: Cron poll, chat prompt

**Trigger Routing Policy（触发路由策略）**:
决定一条 `Trigger` 只做本地提醒、还是进入协同简报等慢通道的规则；v1 默认本地，协同路由受单标的冷却、全局并发与令牌桶约束，超限即降级为纯数值卡，不排队、不轮询模型。
_Avoid_: Notification rule, auto-prompt

**Briefing（扫描简报）**:
由扫描器 `Trigger` 触发、绑定触发时冻结的 `Evidence Snapshot`、经协同宿主生成的只读解读卡与收件箱项；不是定时报告，也不是交易指令。
_Avoid_: Scan report, alert summary, cron digest

**AGENT_ANNOTATION（智能体标注层）**:
图表上经硬门控校验的只读标注图层，必须引用同一冻结快照的 `level_id` 或具体 K 线时刻，并与人工画线 `HUMAN_DRAWING` 分离；禁止拖拽，采纳后才进入人工图层。
_Avoid_: AI drawing, overlay guess

**Setup Insight（战术交易洞察）**:
认知层或策略基于单一证据快照产出的结构化方向观点，包含入场价格带、逻辑证伪止损价、止盈目标、持仓时间上限、洞察有效期与证据引用，本身不是订单。
_Avoid_: Trade order, buy/sell call, raw signal

**Controller Patch（控制器参数补丁）**:
慢层认知模块根据市场状态（Regime）签发的、严格限定在预设安全上下界内的快层确定性策略参数更新包。
_Avoid_: Hot code injection, free prompt rule

**Abstain（显式弃权）**:
当证据缺失、数据陈旧、逻辑冲突或推理超时时，信号提供方明确声明不产生方向性交易观点的合法输出状态。
_Avoid_: Default hold, silent fallback

**Staged Plan Draft（暂存交易草案）**:
智能体在人机协同模式下产出的待审阅交易方案草稿，保存在常驻服务中可反复编辑；只有交易员确认后的不可变版本才会提交给账户进程，经定仓与风控审批。
_Avoid_: Pending order, unverified execution

**Decision Origin（决策来源归因）**:
标识每笔获批执行计划决策形成路径的互斥分类标签，严格区分为纯人工独立发起（`HUMAN`）、人机协同审阅共创（`CO_PILOT`）与智能体自治生成（`AGENT`）。
_Avoid_: User tag, strategy name

## 组合、风控与执行

**Sub-Account（虚拟子账户）**:
在同一套行情底座上初始本金、保证金核算、风险单元配额与系统交易状态彼此物理隔离的独立账户核算单元。
_Avoid_: User profile, strategy folder

**Risk Unit（风险单元 `R`）**:
单笔交易计划在触及逻辑证伪止损价时允许承受的最大账户净值亏损额度，是跨品种统一反推建仓数量的基准单位。
_Avoid_: Fixed lot, arbitrary weight

**Approved Execution Plan（获批执行计划）**:
通过组合定仓与确定性硬风控校验、具备唯一身份且自包含入场规则与退出保护屏障的完整交易计划，是进入执行模块新建或扩大仓位的唯一合法输入。
_Avoid_: Raw order, LLM JSON command

**Position Round（仓位闭合回合）**:
同一虚拟子账户在同一交易标的上从零建仓、同向合并加仓直至全部平仓或强平归零的单一方向完整持仓生命周期。
_Avoid_: Overlapping ticket, hedge leg

**Exit Barrier Set（四重保护屏障）**:
绑定在单一仓位回合上的本地自治退出边界集合，包含硬止损、止盈目标、最长持仓时间垂直边界以及可选的激活式移动止损规则；同向加仓沿用回合现有屏障。
_Avoid_: Triple Barrier, manual stop, next-turn LLM exit

**Position Executor（仓位生命周期执行器）**:
在确定性执行层内独立托管单一仓位回合从挂单建仓、部分成交、同向加仓合并到触发保护屏障平仓全过程的有限状态机。
_Avoid_: Order wrapper, stateless webhook

**Risk Reservation（风险预留）**:
已通过组合风控放行、但尚未在权威账本中可见的风险单元、保证金与仓位回合占用，在对应执行事件入账或计划撤销后释放，用于防止并发计划基于过期账户状态重复放行。
_Avoid_: Pending cache, soft lock

**Trading State（系统交易状态）**:
控制执行出口放行范围的三态全局或账户级开关，严格区分为正常交易（`ACTIVE`）、仅允许合规减仓（`REDUCING`）与停发新单（`HALTED`）。
_Avoid_: Kill switch, pause button

## 账本、延迟与评测

**Ledger（权威资金账本）**:
以精确十进制记录现金、保证金占用、持仓、成交、手续费与资金费率收支的唯一账户事实源，严格独立于智能体对话历史与经验记忆。
_Avoid_: Agent memory, portfolio cache

**Decision Latency（决策延迟 `Δt`）**:
从计划提交时冻结的证据快照时刻到执行计划最终抵达执行层之间的真实或模拟耗时，模拟器必须越过该时间偏移后的盘口方可判定成交；人工审阅草案的时长不计入其中。
_Avoid_: Zero-delay fill, bar-close fill

**Shadow Run（实时影子模拟）**:
接入真实实时市场行情流与人机完整分析链路、但在执行接口后由本地高保真模拟器撮合记账而不向外部场所发送真实订单的验证运行态。
_Avoid_: Toy backtest, dry-run print

**Evaluation Evidence Tier（评估证据等级）**:
按所用模型训练数据截止日对智能体评估结果划分的可信等级，严格区分实时前向（`SHADOW_FORWARD`）、截止日之后回放（`POST_CUTOFF_REPLAY`）与仅供诊断的截止日之前回放（`DIAGNOSTIC`），只有前两类可作晋升依据。
_Avoid_: Backtest score, contamination-free claim

**Trade Journal（交易复盘日志）**:
将已执行计划、当时盘面与图表快照、人工主观笔记、智能体推理轨迹及全成本归因结果绑定在一起的结构化复盘档案。
_Avoid_: Trading diary, session transcript

**Co-Pilot Lift（人机协同净增值）**:
人机协同计划的实际全成本净收益，减去同一智能体原始暂存草案在隔离反事实沙箱中模拟得出的基准净收益后的边际贡献差值。
_Avoid_: Win rate diff, manual PnL
