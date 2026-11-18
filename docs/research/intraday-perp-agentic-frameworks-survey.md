# 日内、永续合约与多资产通用架构：Agentic 交易框架与量化引擎补充调研

- **调研日期**：2026-11-18（Asia/Shanghai）。
- **定位**：作为 [agentic-trading-systems-survey.md](./agentic-trading-systems-survey.md)（聚焦日频股票研究原型）的姊妹篇，专门针对 **加密永续合约 + 美股日内交易 + 跨品种通用软件架构** 目标，补充核查 2024–2026 年日内/多资产智能体交易框架及四大成熟量化引擎的模块解耦设计。
- **方法**：区分公开论文与官方仓库已实现事实、日内/永续场景下的工程限制，以及对 Talaria 模块设计的推导结论。未接入真实资金账户或运行实盘策略。

---

## 1. 核心结论

1. **日内与永续场景必须抛弃「自由多角色聊天直接管订单」**：在分钟级行情与杠杆保证金账户下，多智能体自由辩论会线性放大推理延迟（`Δt`）与 Token 成本，并在剧烈波动中因跨轮次响应滞后造成止损穿仓（如 Nof1 Alpha Arena 实盘基准复盘所示）。
2. **认知层最佳实践收敛为三项机制**：
   - **层级合成与通信裁剪**（`FinCon`）：分析师各自独立处理单源数据并只向合成者输出结构化结果；
   - **异动扫描前置与上下文组装**（`NOFX`）：先用纯数值扫描器按持仓量（OI）、成交量、费率与跳空筛选活跃标的，再唤醒模型；
   - **战术洞察与参数调优双轨并行**（`FinCon/M3FT` + `TiMi`）：中低频结构/事件机会输出单笔战术洞察，高频微结构逻辑由慢层模型周期性校准纯代码控制器参数。
3. **通用软件引擎最佳实践收敛为「流水线解耦 + 特征接口 + 自治执行器」**：
   - 借鉴 QuantConnect `LEAN` 的五阶段流水线（`Universe → Insight → Portfolio → Risk → Execution`）；
   - 借鉴 `VeighNa (vn.py)` 与 `NautilusTrader` 的事件总线、定点精度值对象与统一网关/特征建模；
   - 借鉴 `Hummingbot Strategy V2` 的 **`PositionExecutor` + Triple-Barrier（止损、止盈、时间上限、移动止损）**，把单笔日内交易的盯盘退出从大模型轮询中彻底剥离。

---

## 2. 前沿 Agentic 交易框架对比（2024–2026）

| 框架 / 项目 | 组织与通信模式 | 核心机制事实 | 借鉴价值与工程边界 |
|---|---|---|---|
| **FinCon** | Manager–Analyst 树状层级 | 裁剪分析师横向对话；提出概念语言强化（CVRF）在周期结束后更新高层信念文本；结合日内/日度 CVaR 控制风险。[FC-paper]、[FC-repo] | 验证了减少冗余群聊可同时提升决策信噪比并降低推理耗时；原实现仍面向股票序列，不含永续保证金与盘口撮合。 |
| **HedgeAgents** | 基金经理 + 分资产对冲专家（BTC / 美股 / 外汇） | 通过预算分配会（BAC）、经验共享会（ESC）和极端行情紧急会议（EMC）做按需跨资产协同。[HA-paper] | 跨市场相关性压降与极端状态降杠杆思路适合「加密 + 美股」组合；但硬熔断仍须由确定性代码在毫秒级先行触发。 |
| **TiMi (Trade in Minutes)** | `Policy → Optimization → Deployment` 快慢解耦 | 慢层由智能体做模式分析、生成与调优策略代码/参数并经回测校验；快层在线以纯确定性代码执行分钟级交易（$c_{\text{bot}} \ll c_{\text{agent}}$）。[TIMI-paper] | 直接解决 1m–5m 级别大模型延迟过高问题；落地时应将输出约束为白名单有界参数包（`Controller Patch`）而非未隔离热代码。 |
| **M3FT & QuantAgent** | `Micro 1 → Macro → Micro 2` 三段式 / 价格序列优先 | `M3FT` 面向多币种共享全仓保证金永续合约：先纯代码构建对齐数值状态，再由模型解释状态与跨币种传染风险，最后合并数值与语义优化目标敞口；`QuantAgent` 拆分 `Indicator / Pattern / Trend / Risk` 模块。[M3FT-paper]、[QA-paper] | 证明日内与永续场景应以索引对齐的数值结构为主输入，避免仅靠滞后新闻长文本决策。 |
| **NOFX (`NoFxAiOS/nofx`)** | Go 模块化后端 + TS 前端，支持 Binance / Hyperliquid 永续与美股 | 拆分为候选扫描器（OI 激增、成交量异动）→ 交易上下文组装器（账户净值、强平价、5m/15m/1h/4h K 线、Funding、多空比）→ 半自动 `Frontier` / 全自动 `Autopilot` → 硬风控解析与交易所运行时。[NOFX-repo] | 异动扫描漏斗与多维永续上下文组装极具实战参考价值；但缺乏严格双时间 PIT 快照回测引擎，且自托管部署必须强制本地鉴权。 |
| **Open-Finance-Lab / AgenticTrading** | 模块化编排 + 标准化回测台 | 显式追踪推理延迟成交偏移（Decision-Latency Fills）与交接错误，量化模型延迟对日内成交价格的真实损耗。[OFL-repo] | 为评测模块强制引入 `t_eligible = t_snapshot + Δt` 延迟墙提供了直接基准依据。 |

---

## 3. 成熟多资产量化引擎模块解耦对比

| 引擎 | 核心解耦范式 | 对 Talaria 通用可扩展架构的具体贡献 |
|---|---|---|
| **QuantConnect LEAN** | `Universe → Alpha(Insight) → Portfolio → Risk → Execution` 五阶段接口 [LEAN-fw] | 确立了信号层只发带有效期的 `Setup Insight`、不直接接触仓位与订单的标准分层。 |
| **Hummingbot Strategy V2** | `Controller`（策略大脑）与 `PositionExecutor`（自治生命周期状态机）分离 [HB-v2]、[HB-tb] | 提供了日内方向性交易最干净的 `Execution Seam` 形态：在执行层内部闭环托管止损、止盈、时间上限与移动止损四重屏障。 |
| **VeighNa (`vn.py`)** | `EventEngine + MainEngine + BaseGateway + BaseApp` [VNPY-doc] | 展示了轻量 Python 微内核如何通过统一事件模型与薄网关扩展多市场连接器。 |
| **NautilusTrader** | Rust 单线程确定性消息总线、定点精度值对象、`CryptoPerpetual` 保证金与 `FundingSettlement` 机制 [NT-perp]、[NT-margin] | 提供了永续合约保证金、标记价格强平与整点资金费率结算的精确领域建模参照；其自身可作为未来实盘 `Execution Seam` 后的候选重型后端。 |

---

## 4. 对 Talaria 设计基线的落地映射

综合上述核查结论，Talaria 在正式架构规范（`docs/architecture.md`）中固定以下模块收敛：
1. 在数据层之后增设独立的 **`M02 scanner`**（吸收 `LEAN Universe` + `NOFX Candidate Scanner`）；
2. 在 `M03 signals` 中统一支持 **`Setup Insight`（战术单）** 与 **`Controller Patch`（TiMi 调参）**，并强制要求非弃权洞察携带逻辑证伪止损价 `invalidation_price`；
3. 在 `M04 portfolio_risk` 中采用基于止损距离的 **`Risk Unit (R)` 定仓** 与跨市场相关性簇上限，取代简单股票净值百分比权重；
4. 将 **`M05 execution` 的替换 Seam** 建立在完整执行计划与四重屏障 `Position Executor` 之上（模拟优先阶段仅实现含 `Δt` 延迟墙、挂吃单滑点与 `Funding Settlement` 的 `SimExecutionAdapter`）。

---

## 5. 来源索引

- [FC-paper]: FinCon: *A Synthesized LLM Multi-Agent System with Conceptual Verbal Reinforcement for Enhanced Financial Decision Making* (NeurIPS 2024 / arXiv:2407.06567)
- [FC-repo]: https://github.com/pipiku915/FinCon
- [HA-paper]: HedgeAgents: *A Balanced-aware Multi-agent Financial Trading System* (arXiv:2502.13165)
- [TIMI-paper]: TiMi: *Trade in Minutes: A Rationality-Driven Multi-Agent Framework for Minute-Level Quantitative Trading* (arXiv)
- [M3FT-paper]: M3FT: *Micro-Macro-Micro Reasoning for Multi-Agent Cryptocurrency Perpetual Futures Trading* (arXiv)
- [QA-paper]: QuantAgent: *Price-Driven Multi-Agent LLMs for High-Frequency Trading* (arXiv)
- [NOFX-repo]: https://github.com/NoFxAiOS/nofx
- [OFL-repo]: https://github.com/Open-Finance-Lab/AgenticTrading
- [LEAN-fw]: https://www.quantconnect.com/docs/v2/writing-algorithms/algorithm-framework/overview
- [HB-v2]: https://hummingbot.org/strategies/v2-strategies/
- [HB-tb]: https://hummingbot.org/strategies/v2-strategies/executors/positionexecutor/
- [VNPY-doc]: https://www.vnpy.com/docs/cn/index.html
- [NT-perp]: https://nautilustrader.io/docs/latest/concepts/instruments/
- [NT-margin]: https://nautilustrader.io/docs/latest/concepts/accounting/
