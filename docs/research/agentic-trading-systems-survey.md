# Agentic Trading System：框架、组件与工程边界调研

- 调研日期：2026-11-15。
- 范围：公开的一手论文、官方文档和官方仓库源码；重点是系统怎么分层、组件怎么协作，以及研究原型距离真实交易还缺什么。
- 方法：源码优先，区分「已实现」「论文设计」「路线图」「本文建议」。以下是架构调研，不是收益复现或生产安全审计；没有连接券商账户、执行交易或调用付费金融数据接口。
- 适用边界：样本以股票研究、日频/低频策略和部分加密资产实验为主，不能直接外推为所有资产或高频交易的通用答案。

## 1. 核心结论

**更有用的理解不是“让一群 LLM 讨论后买卖”，而是：研究/决策代理，接到一套受约束、可恢复、可审计的交易系统上。** 这是对本次样本的工程归纳，不是所有项目已经遵循的统一标准。[TradingAgents 图结构][TA-graph]、[RD-Agent 研究循环][RD-loop]和 [ai-hedge-fund 交易流水线][HF-stages]分别展示了不同部分。

需要先区分三种产物：

1. **分析与观点**：报告、看多/看空、buy/sell/hold；未必包含资金、仓位、成交模拟。
2. **策略与组合决策**：可执行策略代码、目标权重、风险预算；仍不是订单成交。
3. **真实运行的交易账户**：订单状态机、部分成交、账本、券商对账、恢复与权限控制。

很多名称包含 trading/hedge fund 的开源项目只覆盖其中一部分。反过来，NautilusTrader 这类交易基础设施很重要，但本身不是一个 LLM 多代理投研团队。[HF-stages]、[NT-architecture]

**最值得借鉴的组合方式：离线研究循环 + 在线决策循环 + 独立的确定性资金控制层。** 下文第 4—7 节是本文建议，不表示任一项目开箱即用地实现了全套能力。

## 2. 代表框架实际上在做什么

| 类别 / 代表 | 组织方式 | 主要产物 | 借鉴点与边界 |
|---|---|---|---|
| 多角色决策：**TradingAgents** | 分析师并发 → 多空研究辩论 → Trader → 风险讨论 → 组合经理裁决 | 分析、提案和五档投资评级 | 有界 LangGraph 图；当前 backtest 明确是独立决策质量评价，不含成交数量、成交价、现金账本。[TA-graph]、[TA-backtest] |
| 金融研究工作流：**FinRobot** | V0 AutoGen；V1 报告 Web 应用；V2 PydanticAI 投研桌面系统 | 数据查询、估值计算、研究报告工件 | V2 可证 Lead + 五个子角色、七条流水线；另有 SMA 回测工具，不等于报告直接驱动交易。[FR-versions]、[FR-roles]、[FR-pipelines] |
| 记忆/反思型：**FinMem、FinAgent** | FinMem 单 Agent 分层记忆；FinAgent 五模块认知流程 | 有历史上下文的动作建议 | FinMem 的 short/mid/long/reflection 与 FinAgent 的五模块是两套设计，不能混成一个“多角色团队”。[FM-agent]、[FA] |
| 量化研发：**Microsoft RD-Agent / R&D-Agent-Quant** | 假设 → 实验设计 → 编码 → 运行 → 反馈；区分因子与模型路径 | 因子/模型代码、实验与反馈 | 自动化研究，而不是直接让 LLM 管每一笔订单；Qlib runner 是实际组件。[RD-loop]、[RD-conf] |
| 基金运行内核：**ai-hedge-fund 当前主线** | Fund → Strategy → AlphaModel；评估、风控、执行、账本分层 | Signal → 目标权重 → 差量订单 → 成交/会话记录 | 相比旧版人格化分析师演示，更值得看它统一的 backtest/paper 路径；实盘 broker 仍在路线图。[HF-core]、[HF-roadmap] |
| 编排运行时：**LangGraph** | State、Node、Edge，加持久化、恢复和人工中断 | 可执行工作流/Agent 状态 | 是通用编排底座，不是交易账本、券商适配器或风险引擎。[LG-overview]、[LG-persistence] |
| 工具与执行：**Alpaca MCP、NautilusTrader** | 前者暴露券商工具；后者提供交易运行内核与适配层 | API 工具调用 / 交易命令与事件 | 解决不同问题，不能把“支持 MCP”理解成“已具备安全自主交易”。[AM]、[NT-architecture] |

### 2.1 RD-Agent：研究 Agent 与交易 Agent 应分开

固定源码的 `QuantRDLoop` 注入 hypothesis generator、factor/model experiment converter、coder、runner、summarizer；实际阶段是 `direct_exp_gen → coding → running → feedback`。配置将 runner 指向 Qlib 的因子/模型实验执行器。[RD-loop]、[RD-conf]

它回答的是：**能否自动提出并验证新的 alpha 假设？** 不直接回答：**如何防止重复订单、保证账户一致性、恢复中断的实盘？** 因此适合放在离线研究区，将产物送入独立验证和发布门禁，而非给予研究 Agent 实盘权限——后半句是本文建议。

### 2.2 ai-hedge-fund：观点与订单之间多了一套确定性内核

本次固定主线中：

- `AlphaModel.predict(ticker, date, data_client) -> Signal` 是统一观点接口；LLM 模型和纯数值模型可以共存，观点与仓位计算分开。[HF-alpha]
- `assess_fund` 获取各策略/模型观点，组合、净额化，调用风险限制并验证目标；返回 `DecisionRecord`，这一阶段不拿 broker 下单。[HF-stages]
- `execute_decision` 验证时序和 mandate，按账户与价格算订单，再检查投影仓位和总敞口；`build_orders` 由目标股数减当前股数产生差量订单。[HF-stages]、[HF-orders]
- `advance` 先检查停机、顺序和行情完成性，再对账，然后执行待处理决策、估值、生成新决策。日频路径在 T 收盘形成决策，后续 session 执行，而不是用同一收盘同时分析并成交。[HF-session]
- Backtest 与 paper 复用核心流程；paper 的持久账本是有价值的设计参考，但官方路线图仍把 live broker 列为后续工作。[HF-core]、[HF-roadmap]

**限制：共享代码不等于真实成交模型，也不能自动证明没有时间泄漏。** 例如源码的订单构造是按目标权重/净值/价格换算整数股，执行阶段依赖 session 价格与 broker 模型。用于实盘仍需验证价格可得性、部分成交、流动性与订单恢复，而不是仅更换 API 地址。[HF-orders]、[HF-stages]

### 2.3 记忆型不是多代理型

FinAgent 论文的五模块分工是：

```text
市场情报 ───┐
低层反思 ───┼─> 各自的记忆存储/检索 ─> 工具增强的决策
高层反思 ───┘
```

低层反思关注市场信息与价格变化，高层反思关注先前决策及结果；决策结合工具和交易者偏好。向量记忆提供上下文，不意味着模型权重已在线训练，更不意味着记忆里推导出的观点已经成为账户事实。[FA]

### 2.4 三个容易被旧介绍误导的实现细节

- **TradingAgents 当前的核心记忆路径不是向量库。** 固定版本采用 Markdown Memory Log，记录待结算决策并在历史查询中过滤 `resolved <= as_of` 的已成熟经验。它的最终输出是评级/提案，源码回测明确不是组合模拟器；README 的“模拟交易所执行”不能代替运行路径证据。[TA-memory]、[TA-backtest]
- **FinRobot 不应再一概概括为 AutoGen。** 当前 V2 核查到 Lead 与 data/analysis/modeling/synthesis/report 五个子角色，七条固定研究流水线；估值的典型数值计算由 Python 算子承担，LLM 负责后续论述。README 的九 Agent/Production 标签没有被本次核查全量证实。它暴露的回测工具运行 SMA crossover，适配器有美股市场限制和零摩擦警告，不是 Agent 研报策略已获收益验证。[FR-versions]、[FR-roles]、[FR-pipelines]、[FR-compute]、[FR-tool]、[FR-backtest]
- **FinMem 的“训练”要按代码解释。** Train 有意把次日价差用于反思和动作构造；Test 分支明确移除该 future record。因此训练曲线不能作为可交易收益，也不能反过来声称 Test 必然直接读取未来。其简化 Portfolio 不是完整现金/成本/撮合账本。[FM-train]、[FM-test]、[FM-portfolio]

完整组件与逐行证据见[三框架源码核查](<./agentic-trading-frameworks-sources.md>)。

## 3. 一个常见误区：Agent 数量不等于系统能力

LangGraph 官方区分：workflow 走预先定义的代码路径；agent 可以动态决定流程和工具使用。因而“多个角色按固定图依次发言”可以是一个多角色工作流，不必解释成多个独立自治交易者。[LG-workflows]

设计时更有价值的问题是：

- 这个节点掌握什么独立信息？
- 产物是否是下游可验证的结构，而不是一大段自然语言？
- 它是否有权改变资金、订单、风险限额？
- 失败、超时、相互矛盾时，系统采取什么动作？
- 增加它后，收益/风险/信息质量的增益是否覆盖推理成本与时延？

以上是本文的选型与验证问题。**不建议默认堆叠十几个分析师人格；先有最小闭环，再验证新增角色是否有独立贡献。**

## 4. 建议参考架构：两个循环，共用受控底座

```text
                     离线研究循环
研究问题 → Research Agent → 因子/策略代码 → 沙箱运行与历史回测
    ↑                                      ↓
    └──────────── 实验反馈 / 研究记忆 ────────┘
                                           ↓
                              独立样本验证 / 审批 / 版本注册
                                           ↓ 已批准的策略版本
                     在线决策与交易循环
行情/新闻/财报 → 时间可追溯的数据视图 → 分析 Agent / 数值模型
                                           ↓ Signal / Proposal
                                 组合构建与资金分配
                                           ↓ PortfolioTarget
                                 确定性风控 + 必要审批
                                           ↓ ApprovedIntent
                            持久化命令 → OMS/执行器 → 券商
                                           ↑             ↓
                               对账 / 订单状态 ← 成交与拒单
                                           ↓
                                账本、归因、监控与复盘
```

设计来源分别是 RD-Agent 的实验循环、ai-hedge-fund 的观点/执行分离和交易引擎的命令/事件模型；**此图是本文综合方案，不是任何单一仓库的复刻。**[RD-loop]、[HF-stages]、[NT-architecture]

核心权限边界：研究 Agent 可以提出假设、生成代码；分析 Agent 可以提出观点；**只有受控执行服务能使用交易凭据，只有独立政策流程能修改硬风控。** 新闻、网页、研报中的文字是证据输入，不是有权调用工具或修改政策的指令——这是针对工具型 Agent 的工程建议。

## 5. 组件应该怎么拆

以下表格是建议接口，不是已经形成行业统一标准的协议。

| 组件 | 负责什么 / 主要输出 | 必须划清的边界 |
|---|---|---|
| **Data Gateway / PIT Store** | 行情、新闻、财报、标的元数据、交易日历；提供指定决策时点可知的数据快照 | 事件时间、发布时间、实际可获取时间不同；财报所属期不等于公布日；缺失/陈旧数据必须显式处理 |
| **Research Lab** | 假设、代码生成、沙箱实验、配置与结果版本 | 不接实盘密钥；不可把反复查看的测试集仍称为独立样本 |
| **Signal / Analysis Agents** | 基本面、事件、技术、情绪等观点，带证据、期限和可选弃权 | 观点不是仓位；自报 confidence 不等于经过校准的胜率 |
| **Orchestrator** | 状态图、调度、超时、重试、预算、人工中断、故障策略 | 不把整个订单链当成可以任意重跑的普通 LLM 节点 |
| **Portfolio Constructor / Allocator** | 合并相关/矛盾信号，按资金预算和约束形成目标组合 | 多个 Agent 看好同一标的不是多个独立 alpha；需做净额化和相关性控制 |
| **Risk Gateway** | 下单前检查限额、杠杆、集中度、交易权限、流动性、价格/数据新鲜度 | LLM 可以解释风险，不能绕过硬限制；明确限制新增风险与紧急减仓的不同政策 |
| **OMS / Execution Engine** | 将目标差量变为订单，维护状态、撤单、改单、重试、成交处理 | 提交成功不等于成交；未知提交结果不能盲目重试；剩余未成交量必须计入风险 |
| **Ledger / Reconciliation** | 订单/成交/费用/现金/仓位记录；与券商账户核对 | 内部意图与券商实际状态都要保留；发生分歧不得简单用 Agent 记忆覆盖 |
| **Evaluation / Observability** | 净收益、回撤、换手、成本、归因、延迟、模型/数据版本、事故告警 | 不只看一条收益曲线；模型异常与交易故障要有独立指标和处置路径 |

这些边界由样本中不同层次的实现和限制启发：数据与时序见 [HF-session]；信号/组合/风险/订单的分离见 [HF-stages]；编排持久化见 [LG-persistence]；执行基础设施见 [NT-architecture]。更细的券商、重放与恢复限制见第 7 节和[执行层证据笔记](<./agentic-trading-execution-sources.md>)。

### 5.1 建议优先固定的数据契约

```text
EvidenceRef       = source + published_at + available_at + snapshot_id
Signal            = asset + as_of + horizon + view + evidence + abstain
PortfolioTarget   = strategy_version + target_weights + constraints + expires_at
RiskDecision      = policy_version + approved/rejected + reasons + approved_target
OrderIntent       = intent_id + account + asset + side + quantity + price_policy
ExecutionReport   = intent_id + broker_order_id + status + fills + fees + timestamps
```

这里是示意字段，不是推荐照抄的完整金融数据模型。例如期权、多币种、期货保证金和公司行动还需要额外的领域建模。重点是把**推理产物、风险裁决、交易意图、实际结果**分成不同对象，并保留版本和因果关联。

### 5.2 至少分开三类存储

1. **研究/语义记忆**：历史新闻、解释、失败经验、反思，可做向量检索，允许过期或被后续证据修正。[FA]
2. **工作流运行状态**：节点状态、检查点、中断位置、运行参数，用于恢复编排。LangGraph 的 checkpointer 与跨会话 Store 也有明确区别。[LG-persistence]
3. **资金与订单事实**：已提交命令、订单回报、成交、余额、对账结果；不能用“模型上次说已经买了”作为依据。[HF-session]、[NT-architecture]

把三者分开是本文建议；**向量数据库不是资金账本，工作流 checkpoint 也不是券商侧事务。**

## 6. 组合与风控为什么不宜交给自由文本

一个可解释的流程是：

```text
带证据的多源观点
  → 可校准的评分 / 数值预测
  → 策略内组合与策略间资金预算
  → 约束后的目标权重
  → 结合已有仓位、未成交订单的交易差量
  → 下单前再校验
```

其中 Agent 适合处理非结构化信息、生成候选解释或方案；数值计算和强制约束适合由可测试的程序承担。这一分工在 ai-hedge-fund 的 `assess_fund`、`execute_decision` 和 `build_orders` 中已有具体实现参考，但本报告不保证其覆盖所有生产级约束。[HF-stages]、[HF-orders]

特别要避免两个混淆：

- **Risk Agent 的讨论不是硬风控。** 它可以说“风险偏高”，但若没有不可绕过的代码门禁，这句话不能限制实际交易权限。
- **Kill switch 不等于清仓。** 停止新增命令、取消挂单、只允许减仓、主动平仓，是不同操作。应为它们制定独立、可测试的行为，不能用一个模糊的 stop 按钮代替。

后者必须结合实际交易引擎的状态语义验证。例如本次 NautilusTrader develop 快照中，`HALTED` 拒绝新提交/修改，包括经过该风险门禁的已释放保护性止损；`REDUCING` 只允许符合条件的单个 `reduce_only` 提交；`bypass=true` 还会跳过交易状态检查。不能把它们理解成“暂停但一定保留自动保护”，也不能假设所有版本和适配器一致。[NT-risk]

## 7. 从 demo 到实盘，最容易漏掉的工程问题

### 7.1 编排恢复可能重复执行副作用

LangGraph 官方明确说明：`interrupt()` 恢复时节点从头执行。即便把下单放在审批之后，如果外部副作用已发生而节点尚未成功完成就崩溃，重试仍可能重复副作用；需要外部幂等/去重。[LG-interrupts]

**建议：** 在网络下单前持久化命令意图和唯一标识；记录状态转移；遇到超时或未知结果先向券商查询/对账；只有明确满足条件才重发。命令账本/outbox 能缩小丢失窗口，但若券商不支持可靠去重，也不能凭此宣称端到端 exactly-once。

### 7.2 MCP 是工具接口，不是交易安全证明

Alpaca 官方 MCP 项目提供行情、账户、交易等工具接入。工具可被模型调用，只说明接入能力存在，不自动说明策略经过验证、权限已最小化、审批不可绕过或成交已完成。[AM]

具体到本次 Alpaca MCP 快照：`ALPACA_TOOLSETS` 未设置时默认开放全部组；`account` 含 `patchAccountConfig`，`trading` 同时含读写操作，按组过滤不能直接充当严格只读模式。`ALPACA_PAPER_TRADE` 默认是 paper，但只有 `true/1/yes` 被解释为 paper，其他字符串选择 live URL；这是路由风险，不表示 paper 密钥能自动获得 live 权限。[AM-toolsets]、[AM-mode]

**建议：** 研究进程只获得读工具；写权限只给受控执行服务。paper/live 使用明确枚举、启动校验和不同凭据，未知配置拒绝启动。逐工具权限、超时处理与注入边界见[执行层证据笔记](<./agentic-trading-execution-sources.md>)。

### 7.3 引擎内置持久化，不代表券商与本地原子提交

交易引擎、消息总线、缓存、数据库与券商是不同故障域。需要检查实际适配器的重试、客户端订单 ID、事件持久化和恢复语义，不能仅因存在 event sourcing 就假设“下单与落盘要么都发生，要么都不发生”。

本次 NautilusTrader develop 的官方策略文档明确：命令不保证 exactly-once；cache backing 和可选 event store 都有异步持久化路径，发送不等待 durable commit，因而存在 venue 已接单、本地尚未持久化的故障窗口。应用侧仍需稳定命令身份、持久化意图、查询/对账和未知状态处置。[NT-policy] 具体 adapter 限制见[执行层证据笔记](<./agentic-trading-execution-sources.md>)。

## 8. 评测应该验证什么

本次**不按各项目 README 的收益数字排名**：市场、时期、成本、样本拆分、LLM 版本、数据可得性和成交假设不同；也没有独立复跑这些实验。

建议至少覆盖下面四组测试：

### A. 时间正确性

- 以当时实际可获取时间为准，不只按新闻事件日、财报所属季度过滤。
- 研究记忆与向量检索同样必须按决策时点过滤；反思只使用当时已经观察到的结果。
- 分离观察、决策、发送与成交时间；不能看完整根 K 线再假装在该根开始时成交。
- 对历史 LLM 回测，要额外考虑预训练记忆中的未来信息；日期提示词不是隔离保证。近期 Look-Ahead-Bench 提供了值得关注的诊断思路，但其论文与实现细节还须单独核对，不能把一个基准视作无泄漏认证。[LAB]

### B. 策略有效性

- 明确训练、验证与最终测试；滚动样本外评估，冻结策略与提示词版本。
- 和无 Agent 的基线比较：现金/基准、简单数值策略、单模型、无记忆、无辩论。
- 做机制消融：新闻、记忆、反思、第二位分析师到底增加了什么？
- 计入佣金、价差、滑点、融资/借券成本、容量限制及模型/数据成本；不只展示毛收益。

### C. 执行正确性

- 部分成交、拒单、撤单与成交竞态、限流、断网、重复回调。
- 请求超时但券商已接单、进程在落盘前后崩溃、恢复后重放。
- 人工订单造成的账户偏差、陈旧行情、跨市场交易时段与标的停牌。
- 证明风险约束在异常路径和并发路径仍成立，而非只测试理想路径。

### D. 运维与安全

- 研究代码沙箱化，限制外网/密钥/文件访问。
- 将网页和新闻视作不可信数据，测试提示注入不能获得写工具或修改限额。
- 模型/API 不可用时允许弃权或停发新单，而不是用未经校验的文本兜底。
- 记录数据快照、模型、提示词、策略、风险政策与代码版本；能解释每笔订单为何出现。

上述是本文建议的验收清单，不是声称所有样本已通过这些测试。研究回路、执行时间差与编排副作用分别有直接样本依据。[RD-conf]、[HF-session]、[LG-interrupts]

## 9. 如果以 Talaria 为起点，建议怎么选

以下是低频研究/交易平台的起步建议，**不是已经确认的 Talaria 产品需求**；资产类别、市场、频率和自主权限还需要另行确定。

1. **先实现一个闭环，不先实现一个“组织架构”。** 一个数值基线 + 一个研究/分析 Agent，输出结构化 Signal；保留完全不使用 LLM 的对照。
2. **先建数据时序、账本和 paper runner。** 用同一策略接口跑 backtest 与 paper，同时明确两者成交模型不同；不要把一致代码路径误当成真实成交保证。
3. **组合、硬风控、订单状态机独立于 LLM。** 一个进程或一个仓库也可以，但接口和权限边界要清晰；不必一开始拆成微服务。
4. **编排层按复杂度选。** 需要检查点/人工中断/多分支状态时参考 LangGraph；流程简单时普通 Python 状态机也足够，不为“agentic”而引入框架。
5. **研究自动化后置。** 借鉴 RD-Agent 的假设—实验循环，但产物必须走独立验证与批准，不能自动把本轮最优回测策略上线。
6. **实盘先做执行能力验收。** 可评估 NautilusTrader 或直接券商 SDK 适配器，按市场支持、订单语义与恢复能力选择；Alpaca MCP 可作为工具接入参考，不替代风险网关。

推荐最小模块边界：

```text
market_data | research | signals | portfolio | risk | execution | ledger | evaluation
```

**一句话选型：TradingAgents 看决策分工，FinMem/FinAgent 看记忆与反思，FinRobot 看金融研究工具化，RD-Agent 看自动研究，ai-hedge-fund 看观点到组合的分离，Nautilus/券商适配层看真正的执行与恢复。不要期待某一个仓库同时解决全部问题。**

## 10. 一手来源与版本记录

所有动态网页均于本次调研读取。源码尽量固定到 commit；这只是可追溯快照，不代表官方稳定发布或未来状态。

| 项目 | 本次源码快照 / 资料性质 |
|---|---|
| TradingAgents | `9fa1196fffbcfd9de5146d2dd2869df84ab99838`；详细证据见[框架笔记](<./agentic-trading-frameworks-sources.md>) |
| FinRobot | `63e1884a42ea7b729551a5424b4bd6a65af636e0`；多代实现分开核查，见[框架笔记](<./agentic-trading-frameworks-sources.md>) |
| FinMem | `be814aa47970de9bf2fdd6a1d5a60ae5cf361b46`；训练/测试差异见[框架笔记](<./agentic-trading-frameworks-sources.md>) |
| FinAgent | 作者论文 `2402.18485v3`，第 4 节；本文只据此描述模块，不据此断言生产能力 |
| RD-Agent | `484776c211e4fbbeef03e0ec00d6bbee7362a4f4` |
| ai-hedge-fund | `5c553b5222044f6a5527bd377b4104c50ee0ad28`；当前 `hedge_fund` 核心，不沿用旧目录介绍 |
| LangGraph | 官方在线文档，非固定版本 |
| Alpaca MCP | `9daa38cc41d2327467b8dd83cceb06b457074cbc`；见[执行笔记](<./agentic-trading-execution-sources.md>) |
| NautilusTrader | `12982032fb970cabc085f00169c720792c3edecf`，develop 快照；不等同于稳定 release |
| Look-Ahead-Bench | 作者论文与官方 runner；用途及实现差异见[执行笔记](<./agentic-trading-execution-sources.md>) |

### 来源链接

- [TradingAgents 官方仓库][TA]；版本变化、决策图与回测边界的逐项引用见[框架笔记](<./agentic-trading-frameworks-sources.md>)。
- [FinRobot 官方仓库][FR]；各版本与回测组件的逐项引用见[框架笔记](<./agentic-trading-frameworks-sources.md>)。
- [FinAgent 作者论文，第 4 节][FA]。
- [RD-Agent 量化研究循环][RD-loop]、[组件注入与训练/验证/测试配置][RD-conf]、[作者论文](<https://arxiv.org/abs/2505.15155>)。
- [ai-hedge-fund 核心说明][HF-core]、[路线图][HF-roadmap]、[观点接口][HF-alpha]、[评估与执行分离][HF-stages]、[差量订单][HF-orders]、[会话推进与对账][HF-session]。
- [LangGraph 概览][LG-overview]、[Workflow 与 Agent 的区别][LG-workflows]、[持久化][LG-persistence]、[中断、重试与副作用][LG-interrupts]。
- [Alpaca MCP 官方仓库][AM]、[NautilusTrader 官方架构说明][NT-architecture]、[Look-Ahead-Bench 作者论文][LAB]；关键实现与限制见[执行笔记](<./agentic-trading-execution-sources.md>)。

[TA]: https://github.com/TauricResearch/TradingAgents/tree/9fa1196fffbcfd9de5146d2dd2869df84ab99838
[FR]: https://github.com/AI4Finance-Foundation/FinRobot/tree/63e1884a42ea7b729551a5424b4bd6a65af636e0
[TA-graph]: https://github.com/TauricResearch/TradingAgents/blob/9fa1196fffbcfd9de5146d2dd2869df84ab99838/tradingagents/graph/setup.py#L109-L187
[TA-backtest]: https://github.com/TauricResearch/TradingAgents/blob/9fa1196fffbcfd9de5146d2dd2869df84ab99838/tradingagents/backtest.py#L1-L15
[TA-memory]: https://github.com/TauricResearch/TradingAgents/blob/9fa1196fffbcfd9de5146d2dd2869df84ab99838/tradingagents/memory/log.py#L32-L133
[FR-versions]: https://github.com/AI4Finance-Foundation/FinRobot/blob/63e1884a42ea7b729551a5424b4bd6a65af636e0/README.md#L31-L56
[FR-roles]: https://github.com/AI4Finance-Foundation/FinRobot/blob/63e1884a42ea7b729551a5424b4bd6a65af636e0/finrobot_desktop/finrobot/engine/agents/factory.py#L16-L97
[FR-pipelines]: https://github.com/AI4Finance-Foundation/FinRobot/blob/63e1884a42ea7b729551a5424b4bd6a65af636e0/finrobot_desktop/finrobot/engine/pipelines/registry.py#L65-L144
[FR-backtest]: https://github.com/AI4Finance-Foundation/FinRobot/blob/63e1884a42ea7b729551a5424b4bd6a65af636e0/finrobot_desktop/finrobot/engine/backtest/backtrader_adapter.py#L51-L207
[FR-tool]: https://github.com/AI4Finance-Foundation/FinRobot/blob/63e1884a42ea7b729551a5424b4bd6a65af636e0/finrobot_desktop/finrobot/engine/orchestrator.py#L440-L483
[FR-compute]: https://github.com/AI4Finance-Foundation/FinRobot/blob/63e1884a42ea7b729551a5424b4bd6a65af636e0/finrobot_desktop/finrobot/engine/compute/operators/dcf.py#L48-L170
[FM-agent]: https://github.com/pipiku915/FinMem-LLM-StockTrading/blob/be814aa47970de9bf2fdd6a1d5a60ae5cf361b46/puppy/agent.py#L87-L183
[FM-train]: https://github.com/pipiku915/FinMem-LLM-StockTrading/blob/be814aa47970de9bf2fdd6a1d5a60ae5cf361b46/puppy/agent.py#L460-L462
[FM-test]: https://github.com/pipiku915/FinMem-LLM-StockTrading/blob/be814aa47970de9bf2fdd6a1d5a60ae5cf361b46/puppy/agent.py#L565-L580
[FM-portfolio]: https://github.com/pipiku915/FinMem-LLM-StockTrading/blob/be814aa47970de9bf2fdd6a1d5a60ae5cf361b46/puppy/portfolio.py#L13-L86
[FA]: https://arxiv.org/html/2402.18485v3#S4
[RD-loop]: https://github.com/microsoft/RD-Agent/blob/484776c211e4fbbeef03e0ec00d6bbee7362a4f4/rdagent/app/qlib_rd_loop/quant.py#L30-L128
[RD-conf]: https://github.com/microsoft/RD-Agent/blob/484776c211e4fbbeef03e0ec00d6bbee7362a4f4/rdagent/app/qlib_rd_loop/conf.py#L112-L170
[HF-core]: https://github.com/virattt/ai-hedge-fund/blob/5c553b5222044f6a5527bd377b4104c50ee0ad28/hedge_fund/README.md
[HF-roadmap]: https://github.com/virattt/ai-hedge-fund/blob/5c553b5222044f6a5527bd377b4104c50ee0ad28/ROADMAP.md
[HF-alpha]: https://github.com/virattt/ai-hedge-fund/blob/5c553b5222044f6a5527bd377b4104c50ee0ad28/hedge_fund/signals/base.py#L17-L47
[HF-stages]: https://github.com/virattt/ai-hedge-fund/blob/5c553b5222044f6a5527bd377b4104c50ee0ad28/hedge_fund/pipeline/stages.py#L51-L208
[HF-orders]: https://github.com/virattt/ai-hedge-fund/blob/5c553b5222044f6a5527bd377b4104c50ee0ad28/hedge_fund/pipeline/execution.py#L16-L74
[HF-session]: https://github.com/virattt/ai-hedge-fund/blob/5c553b5222044f6a5527bd377b4104c50ee0ad28/hedge_fund/pipeline/session.py#L132-L245
[LG-overview]: https://docs.langchain.com/oss/python/langgraph/overview
[LG-workflows]: https://docs.langchain.com/oss/python/langgraph/workflows-agents
[LG-persistence]: https://docs.langchain.com/oss/python/langgraph/persistence
[LG-interrupts]: https://docs.langchain.com/oss/python/langgraph/interrupts#side-effects-called-before-interrupt-must-be-idempotent
[AM]: https://github.com/alpacahq/alpaca-mcp-server/tree/9daa38cc41d2327467b8dd83cceb06b457074cbc
[AM-toolsets]: https://github.com/alpacahq/alpaca-mcp-server/blob/9daa38cc41d2327467b8dd83cceb06b457074cbc/src/alpaca_mcp_server/toolsets.py#L9-L159
[AM-mode]: https://github.com/alpacahq/alpaca-mcp-server/blob/9daa38cc41d2327467b8dd83cceb06b457074cbc/src/alpaca_mcp_server/server.py#L101-L132
[NT-architecture]: https://github.com/nautechsystems/nautilus_trader/blob/12982032fb970cabc085f00169c720792c3edecf/docs/concepts/architecture.md#L65-L156
[NT-risk]: https://github.com/nautechsystems/nautilus_trader/blob/12982032fb970cabc085f00169c720792c3edecf/docs/concepts/execution/index.md#L186-L312
[NT-policy]: https://github.com/nautechsystems/nautilus_trader/blob/12982032fb970cabc085f00169c720792c3edecf/docs/concepts/execution/policies.md#L121-L207
[LAB]: https://arxiv.org/abs/2601.13770
