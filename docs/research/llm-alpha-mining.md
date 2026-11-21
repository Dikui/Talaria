# LLM 自动挖因子与自动挖策略：现状、评估可信度与对 Talaria 的适用性

- **调研日期**：2026-11-21
- **调研问题**：LLM 自动挖因子（formulaic alpha / factor mining）与自动生成策略的代表性工作，各自如何产出、如何迭代、如何评估，能否防住过拟合、数据前视和“模型知识截止日”带来的泄露；哪种形态适合 Talaria。
- **方法**：只读一手来源。论文从 arXiv / ACL Anthology 下载 PDF，用 `pdftotext` 抽出全文后逐段核对（文中注明 arXiv 版本号）；仓库用 `git clone --depth 1` 读源码（注明提交号），许可证以 GitHub API 返回值和根目录 `LICENSE` 文件为准；模型截止日只取官方模型页或官方模型卡。综述和检索结果只用来找线索。**没有运行任何系统，也没有复现任何收益数字**，文中数字都是作者自报。
- **标注约定**：
  - **[*]** ＝已对照原文或源码逐字核实。为控制引用篇幅，原文表述大多用中文转述，并注明章节、表号或文件路径，方便回查。
  - **[摘要]** ＝只读了摘要、README 或检索结果，细节未核实。
  - **事实** ＝来源中能直接读到的内容；**判断** ＝本文作者的评估，未经独立验证；**建议** ＝面向 Talaria 的推论。
  - “全文检索无相关讨论”指在抽出的全文中检索 `cutoff / cut-off / look-ahead / lookahead / leak / contamina / memoriz` 等词，没有找到讨论模型知识截止日的段落。这是检索结论，可能漏掉换行断词的写法。
- **与已有调研的关系**：Glasserman & Lin 的匿名化实验、ChronoGPT / TimeMachineGPT、Alpha Arena、StockBench、LiveTradeBench、FINSABER 已在 [llm-trading-lookahead-contamination.md](./llm-trading-lookahead-contamination.md) 核实过，本文只在需要时引用，不重复。

---

## 0. 结论摘要

1. **主流做法已经收敛到同一个回路：LLM 提出假设，写成公式或代码，回测后把指标喂回 LLM，再改。** Alpha-GPT、AlphaAgent、RD-Agent(Q)、QuantaAlpha 和 LLM+MCTS 都是这个结构，区别在于搜索控制（遗传规划、MCTS、多臂老虎机、轨迹变异与交叉）和准入约束（复杂度、原创性、假设一致性）。产出形态以**公式表达式**为主，RD-Agent(Q) 和 QuantAgent 产出 Python 代码；策略/交易类工作（TradingAgents）则由 LLM 直接给出买卖决策。（事实，§1–§9）
2. **几乎所有因子挖掘论文的测试区间都落在所用 LLM 的知识截止日之前或与之重叠。** 例如 QuantaAlpha 用 GPT-5.2（截止 2025-08-31）测 2022-01 至 2025-12；LLM+MCTS 用 GPT-4.1（截止 2024-06-01）测 2021-01 至 2024-11；RD-Agent(Q) 主实验用 GPT-4o / o3-mini（截止 2023-10-01）测 2017–2020。只有 RD-Agent(Q) 的补充实验（2024-01 至 2025-06）和 Alpha-GPT 的 WorldQuant 实时比赛基本落在截止日之后。（事实 [*]，见“对比表”）
3. **讨论过知识截止日的论文很少，讨论的方式也偏弱。** RD-Agent(Q) 换到截止日之后的测试区间重做了实验，并论证 LLM 只看到字段说明、看不到行情；LLM+MCTS 只做了“直接让 LLM 写高收益公式”的探针，结果与随机公式无差异，据此认为没有泄露。后一种探针只能排除“背诵现成好公式”，排除不了“知道哪类因子在测试期有效”这种软泄露。AlphaAgent、FAMA、QuantAgent、QuantaAlpha 全文检索无相关讨论。（事实 [*]；对探针效力的评价是判断）
4. **开源实现里有两处会让“样本外”名不副实，比论文本身的问题更直接。** 第一，RD-Agent 默认 Qlib 模板在 `test` 段上算 IC 和含成本收益，再把这些指标作为反馈交给 LLM 选 SOTA，测试段因此参与了搜索。第二，独立复现（Pan 等，2026）发现 RD-Agent(Q) 原生评估器准入的 57 个因子中有 18 个（31.6%）含时间前视或跨股票排名混用，另外 QuantaAlpha 开源代码的入库规则比论文写的弱。（事实 [*]，§5、§10.1）
5. **LLM 直接做交易决策的证据最弱。** TradingAgents 只测了 5 只股票、约 3 个月，未写明交易成本；开源仓库在论文发布后又陆续修复了多处数据前视缺陷（v0.2.3、v0.3.1、v0.4.0）。Profit Mirage（2025）用 GPT-4o 重测 5 个 LLM 交易智能体，截止日之后 Sharpe 衰减 51%–62%。（事实 [*]，§9、§10.2）
6. **防过拟合手段多为启发式，统计校正几乎缺席。** 常见的有 AST 复杂度上限、与已有因子库的 AST 相似度或输出相关性去重、LLM 打分的过拟合风险等。只有 Pan 等（2026）的 SEADS 用了 Rank-IC t 统计量门槛 max(3.0, Bonferroni 动态值) 和 Fama–MacBeth 检验，并提出在多个历史决策点重跑整个发现流程。没有一篇在搜索全程累计试验次数、做多重检验校正。（事实 [*]；“几乎缺席”是对所读样本的判断）
7. **所有被深读的工作都只测股票（A 股 CSI 300/500/1000、SSE 50、S&P 500、NASDAQ 100），没有加密永续。** 交易成本多按 A 股 0.05% 买入 + 0.15% 卖出设置，没有资金费率。（事实 [*]）
8. **对 Talaria 的判断：** 三种形态中，“LLM 离线生成公式，固化为确定性算子组合”最可行；但只有在 LLM 生成日之后的封存区间或 Shadow Run 中测得的结果，才能当作评估证据。“LLM 定期判断并记录为时间序列”只能前向积累。“LLM 直接交易”只能作为 `SHADOW_FORWARD` 实验。可以借鉴的主要是设计（RD-Agent 的回路结构、AlphaAgent 的 AST 原创性与复杂度约束、SEADS 的验证门与 AST 前视防火墙），能直接复用的代码很少（§11）。（判断）

---

## 对比表

> 放在各节之前，便于先看全貌。“截止日”取官方模型页 [OAI-*] / 官方模型卡 [LLAMA3]；未能从官方来源核到的写“未核实”。

| 项目 | 产出形态 | 回路 | 评估区间 vs 模型截止日 | 防泄露 / 防过拟合措施 | 许可证 / 开源 |
|---|---|---|---|---|---|
| Alpha-GPT（2308.00016v2）| 公式表达式 | 交易想法 → LLM 写公式 → 遗传规划增强 → 回测 → 分析 agent 总结 → 人反馈 | Llama3 70B（截止 2023-12）；离线实验未写日期；WorldQuant IQC 2024 实时参赛（截至 2024-06-25），晚于截止日 | 实时比赛；知识库检索 | 论文未给代码 |
| Alpha-GPT 2.0（2402.09746v1）| 公式 + 模型 + 分析报告 | 三层 agent，人在回路 | 无定量实验 | — | 论文未给代码 |
| QuantAgent（2402.03755v1）| Python 信号函数 | 内循环：写代码 ↔ LLM 评审（查知识库）；外循环：真实回测结果写回知识库 | gpt-4-0125-preview（截止 2023-12-01）；数据为 2023 年 A 股 500 只，**全在截止日之前** | 无；全文检索无相关讨论 | 论文未给代码 |
| FAMA（ACL Findings 2024）| 公式表达式 | 从低相关类别抽因子作上下文示例（CSS）+ 成功经验链（CoE） | text-davinci-002（截止未核实）；附录换 gpt-3.5-turbo（截止 2021-09-01）；测试 2021 全年，与截止日重叠 | 无；全文检索无相关讨论 | 论文未给代码 |
| AlphaAgent（2502.16789，KDD 2025）| 公式表达式 | 想法 agent → 因子 agent（带约束）→ 评估 agent → 反馈 | GPT-3.5-turbo（截止 2021-09-01）；测试 2021-01 至 2025-01，前约 9 个月重叠 | AST 原创性、假设一致性、复杂度约束；**未讨论知识截止日** | 仓库已重写为 A 股框架，**无 LICENSE 文件** |
| RD-Agent(Q)（2505.15155v2）| Python 因子代码 + 模型代码 | 规范 → 假设 → Co-STEER 编码 → Qlib 回测 → 反馈；多臂老虎机选“因子/模型”方向 | 主实验 GPT-4o / o3-mini（截止 2023-10-01），测试 2017-01 至 2020-08，**全在截止日之前**；补充实验 GPT-4o / o4-mini（截止 2024-06-01），测试 2024-01 至 2025-06 | IC≥0.99 去重；LLM 只见字段说明；补充实验换到截止日之后。**默认代码用 test 段指标做反馈** | MIT；仅 Linux + Docker；绑定 Qlib |
| LLM+MCTS（2505.11122v3）| 公式表达式 | MCTS 选节点 → LLM 按维度改写 → 多维评分 → 回传；频繁子树回避 | GPT-4.1（截止 2024-06-01）；测试 2021-01 至 2024-11，约 41/47 个月在截止日之前 | 频繁子树回避；LLM 评过拟合风险；附录 H 做“直接索要公式”探针 | 论文未见代码链接 |
| QuantaAlpha（2602.07085v3）| 公式 + 代码 | 每次挖掘视作轨迹；轨迹级变异与交叉 | GPT-5.2（截止 2025-08-31）；测试 2022-01 至 2025-12-26，约 44/48 个月在截止日之前 | 复杂度与 AST 冗余约束；2021 验证集相关性去重；**全文检索无截止日讨论** | README 徽章写 MIT，**仓库无 LICENSE 文件**；绑定 Qlib |
| Kou 等（2409.06289v4）| 公式（种子因子）+ LLM 选因子 + 组合权重 | 文档 → GPT-4o 抽种子因子 → 多模态 agent 打置信度/风险分 → 选因子 → 动态加权 | GPT-4o（截止 2023-10-01）；测试 SSE50 2023 全年及 2021/22/23 上半年，**全在截止日之前** | 换手上限；**未讨论截止日** | 有 GitHub 仓库，API 未返回许可证 |
| TradingAgents（2412.20138v7）| 每日买/卖/持有决策 | 分析师 → 多空研究员辩论 → 交易员 → 风控辩论 → 基金经理 | o1-preview / gpt-4o（截止 2023-10-01）；测试 2024-01-01 至 2024-03-29，晚于截止日约 3 个月 | 声称只用当日可得数据；未写交易成本 | Apache-2.0 |
| SEADS / AEAP（2609.00731v1）| 公式（基于特征说明）| Planner → 提议 → AST 前视防火墙 → 验证门 → 反思记忆 → 自我演化 | GPT-5.2（截止 2025-08-31）；作者自认存在“软泄露” | 验证门（t≥max(3, Bonferroni)、新颖度 0.7、Fama–MacBeth）、AST 前视防火墙、滚动重跑 | 代码称录用后公开 |

---

## 1. Alpha-GPT 与 Alpha-GPT 2.0（IDEA 研究院等）

来源：Alpha-GPT，arXiv 2308.00016v2（2025-09-20 修订）[AGPT*]；Alpha-GPT 2.0，arXiv 2402.09746v1（2024-02-15）[AGPT2*]。

**做法（事实 [*]）**
- LLM 产出**公式化 alpha 表达式**，算子来自 HXPY 的 19 个基础算子（时序、截面、分组、逐元素），另可调用 scipy/torch 算子（§5.1、表 1）。
- 回路：Trading Idea Polisher 把自然语言想法扩写并消歧；Quant Developer agent 写成公式；Algorithmic Alpha Mining 层用遗传规划做“搜索增强”；回测引擎输出收益、IC、Sharpe；Analyst agent 总结后交给人，人再给下一轮指示（§2、图 2）。大数据库场景用三级 RAG 选字段（图 3）。
- 知识库由 Kakushadze 101 Formulaic Alphas 和一个私有因子库构成，作者称用于提供上下文，不直接复用（§5.1）。
- Alpha-GPT 2.0 把流程扩成 Alpha Mining、Alpha Modeling、Alpha Analysis 三层 agent，人在每层介入。论文是系统描述，**没有定量实验表格** [AGPT2*]。

**评估（事实 [*]）**
- 使用中美两国股票的日内量价数据（OHLCV、VWAP、行业），**实验设置未写时间区间和样本内外切分日期**（§5.1）。
- “翻译一致性”由 GPT-4 给人类研究员和 Alpha-GPT 写的因子打 1–10 分：8.16 对 6.81（表 3）。
- IC 随阶段变化：初始 0.58%，10 轮搜索增强后 1.23%，加 1 轮交互后 2.23%（表 4）。图 4 给出 20 轮迭代的样本内与样本外 IC 曲线。
- 按已结束的 JoinQuant 高频比赛规则重放：Sharpe 5.47，人类第一名 6.88（表 5）。
- WorldQuant IQC 2024 第二阶段（截至 2024-06-25）：81 个合格因子，总分 48866，样本内 65505，样本外 43319；全球第一名样本外 50111（表 2）。
- 离线实验未说明是否扣交易成本；IQC 平台的合格标准包含收益、换手和 Sharpe（§5.4）。

**防过拟合与防前视（事实 [*]）**：没有形式化约束。作者用图 4 样本外 IC 趋稳来论证没有严重过拟合（§5.3）。

**知识截止日（事实 [*]）**：所用模型为 Llama3 70B（§5.1），官方模型卡写明 70B 的预训练数据截止于 2023 年 12 月 [LLAMA3*]。论文关于泄露的唯一表述在 IQC 一段：作者称评估是在官方比赛期间实时进行的，因此保证没有未来信息泄露（§5.4）。离线实验没有讨论截止日。

**判断**
- IQC 实时参赛是本文样本中少有的、从结构上排除模型记忆的评估，有参考价值。但比赛平台的评分细节不透明，样本外分数（43319）也低于全球前十的平均（48715），“超人表现”的说法证据不足。
- 离线实验没有日期、没有成本，“翻译一致性”由另一个 LLM 打分，可信度低。
- 重放已结束的比赛，与模型截止日的关系不明。

**开源与复用（事实 [*]）**：两篇论文都没有给出代码仓库链接。（未见官方仓库；没有进一步检索作者主页）

---

## 2. QuantAgent（IDEA 研究院 / 港科大）

来源：arXiv 2402.03755v1（2024-02-06）[QAG*]。

**做法（事实 [*]）**
- LLM 产出**按固定模板写的 Python 信号函数**，形式仿照 FunSearch（§5.2）。
- 两层循环：内循环中，写手 LLM 根据知识库写代码，评审 LLM 查知识库后打分并反馈；外循环把代码放到真实市场数据上回测，结果写回知识库（§3、图 2）。作者给了基于悲观原则离线 RL 的理论分析（§4）。
- 交易想法由另一个 LLM 生成，作为采样分布（§5.2）。

**评估（事实 [*]）**：A 股 500 只股票，**数据只覆盖 2023 年**（§5.2）。指标为 IC，以及把知识库作为特征的 XGBoost 的 IC 和 Sharpe；另用 LLM 两两比较“信号是否符合交易想法”，构建胜率排行（§5.3）。正文没有写交易成本和样本内外切分。

**防过拟合与防前视（事实 [*]）**：没有专门措施。

**知识截止日（事实 [*]）**：模型为 gpt-4-0125-preview（§5.2）。OpenAI 官方页 `gpt-4-turbo-preview`（指向 0125 版本）给出的截止日为 2023-12-01 [OAI-GPT4T*]。**评估数据完全落在截止日之前。**全文检索无相关讨论。

**判断**：评估只覆盖一年，且在模型知识范围之内，没有成本，没有明确的样本外。只能说明“自我改进回路能让训练期指标上升”，不能说明信号在样本外有效。Profit Mirage 用 GPT-4o 重测 QuantAgent，截止日之后 Sharpe 衰减 51.48%（§10.2）。

**开源（事实 [*]）**：论文没有给出代码链接。

---

## 3. FAMA（人民大学等，ACL Findings 2024）

来源：ACL Anthology 2024.findings-acl.233 [FAMA*]。

**做法（事实 [*]）**
- LLM 产出**公式化因子**，起点是 Alpha101（§4.1）。
- Cross-Sample Selection（CSS）：先把已有因子聚成 7 类，从低相关的类别中各抽样，作为上下文示例，以减轻生成结果同质化（§3、§4.1）。
- Chain-of-Experience（CoE）：把过去挖掘成功的路径作为经验链，引导 LLM 往新方向探索（§3）。
- 温度设为 0，max_tokens=1500（§4.1）。

**评估（事实 [*]）**
- 数据为 S&P 500 成分股 OHLCV，2015-01-01 至 2022-01-01。训练 2015–2019，验证 2020，测试 2021（§4.2）。
- FAMA 自身只用 2020-06-01 至 2021-01-01 的数据，作者称这相当于训练集的 10%（§4.2）。
- 结果：RankIC 0.054、RankICIR 0.485（表 1）；组合年化 38.4%、波动率 4.9%、Sharpe 一栏为 667.2%（表 2）。
- 正文没有写交易成本。

**防过拟合与防前视（事实 [*]）**：只有 RankIC>0.01 的准入阈值（§4.4）；没有复杂度约束，也没有多重检验校正。局限一节只讨论幻觉问题。

**知识截止日（事实 [*]）**：主模型为 text-davinci-002（§4.1）。附录 C 换用 gpt-3.5-turbo 和 text-davinci-003，结果接近（表 4）。OpenAI 官方页给出 gpt-3.5-turbo 截止日为 2021-09-01 [OAI-35*]。text-davinci-002 已下线，当前官方模型页查不到它的截止日（**未核实**）。测试期 2021 全年与 gpt-3.5-turbo 的知识期有约 9 个月重叠。全文检索无相关讨论。

**判断**：测试期只有一年，与模型知识期重叠，没有成本。年化 38.4% 而波动率只有 4.9%，按年化收益除以波动粗算，Sharpe 约为 7.8；表中“667.2%”的口径不明（是否扣无风险利率、是否日度未年化）。这组数字可信度低。

**开源（事实 [*]）**：论文没有给出代码链接。

---

## 4. AlphaAgent（KDD 2025）

来源：arXiv 2502.16789 [AA*]；仓库 `RndmVariableQ/AlphaAgent`（GitHub API 于 2026-11-21 查询）[AA-REPO*]。

**做法（事实 [*]）**
- 三个 agent 组成闭环：想法 agent 按结构化模板提出市场假设；因子 agent 写**公式表达式**并解析为 AST；评估 agent 回测并把结果反馈给想法 agent（§3.3）。
- 三类正则化，合成为目标函数中的 ER(f, h) 项（式 8）：
  - **原创性**：新因子的 AST 与因子库（如 Alpha101）中每个因子求最大公共子树，取最大相似度 S(f)（§3.2.1、图 2）。
  - **假设一致性**：由 LLM 判断“假设 → 描述 → 公式”三者是否一致（§3.2.2）。
  - **复杂度**：符号长度、自由参数个数，以及所用原始特征数的对数（§3.2.2、式 8）。

**评估（事实 [*]）**
- 用 Qlib 框架；CSI 500 数据来自 Baostock，S&P 500 数据来自 Yahoo Finance，只用 OHLCV（§4.1.2）。
- 两个市场切分相同：训练 2015-01 至 2019-12，验证 2020，测试 2021-01 至 2025-01（表 1）。
- 新因子与 4 个基础因子一起输入 LightGBM（最大深度 4）预测次日收益。组合按 top-k dropout 构建：持有 50 只，每次剔除排名最低的 5 只。
- **扣交易成本**：CSI 500 买入 0.0005、卖出 0.0015；S&P 500 只收卖出 0.0005（§4.1.2）。
- 指标为 IC、ICIR、年化收益、IR、MDD（表 2）。基线包括 LSTM、Transformer、LightGBM、AlphaForge、RD-Agent，以及 best-of-10 的 o1 和 DeepSeek-R1。

**防过拟合与防前视（事实 [*]）**
- 有上面三类约束，没有多重检验校正。
- 图 5 是“CSI 500 测试段上各轮的 IC 演化”（§4.4）。**论文没有写明迭代时反馈给 LLM 的指标取自哪一段**；如果来自测试段，测试段就参与了搜索（**未核实**，现仓库已重写，无法对照论文期代码）。

**知识截止日（事实 [*]）**：主模型 GPT-3.5-turbo（§4.1.2），官方截止日 2021-09-01 [OAI-35*]。测试期 2021-01 至 2025-01，前约 9 个月重叠。对照组 RD-Agent 用 GPT-4-turbo，o1、DeepSeek-R1 的截止日更晚，与测试期重叠更多。全文检索无截止日讨论。

**判断**：在本文样本中，这是约束设计最完整的公式挖掘方案之一，测试期 4 年，也扣了成本，比 FAMA、QuantAgent 可信。但两点削弱其结论：一是主模型与对照模型的截止日不同，比较不公平；二是 Pan 等（2026）独立重实现后，AlphaAgent 在两个美股面板上的 Sharpe 处于末位一档（0.82±0.03），与 RD-Agent(Q) 并列，低于 SEADS、Beyond Prompting 和 QuantaAlpha（§10.1）。

**开源与复用（事实 [*]）**
- 仓库在 2026-07 重写为面向 A 股（中证 1000）的多因子研究框架：用 Tushare 或作者发布的数据包，因子用 DSL 表达，挖掘部分为可选的 AgentScope agent [AA-REPO*]。
- README 只写了“代码开源”，**根目录没有 LICENSE 文件，GitHub API 返回许可证为空**。按默认著作权，不能直接拷贝代码。
- 数据绑定 Tushare（需 token），或使用百度网盘数据包。

---

## 5. RD-Agent(Q)（微软亚洲研究院）

来源：arXiv 2505.15155v2（2025-09-25）[RDQ*]；仓库 `microsoft/RD-Agent`，提交 `484776c`（2026-09-23）[RD-REPO*]；Qlib 源码 `qlib/workflow/record_temp.py`、`qlib/contrib/model/gbdt.py`（main 分支）[QLIB*]。

**做法（事实 [*]）**
- LLM 产出 **Python 因子代码**和**模型代码**。
- Research 端：规范单元 → 假设生成 → 任务拆解。Development 端：Co-STEER 编码 agent 写代码并自我修正 → Qlib 回测 → 反馈单元诊断结果并给出改进建议。
- 多臂老虎机调度器决定下一轮优化因子还是模型（§2、§3）。
- 去重：新因子与 SOTA 因子库逐时间片求相关后取均值，最大值 ≥0.99 视为冗余并剔除（§2.4）。

**评估（事实 [*]）**
- 主实验：CSI 300，训练 2008-01-01 至 2014-12-31，验证 2015–2016，测试 2017-01-01 至 2020-08-01（§3）。
- 交易成本：买入 0.05%，卖出 0.15%，单笔最低 5 元，涨跌停阈值 0.095，按收盘价成交（附录 C.4.2）。
- 指标：IC、ICIR、Rank IC、Rank ICIR、ARR、IR、MDD、Calmar（§3）。
- 结果：o3-mini 版 R&D-Agent(Q) 的 IC 为 0.0532（表 1）。
- 补充实验（附录 D.1）：CSI 500 和 NASDAQ 100，训练 2008–2021，验证 2022–2023，测试 2024-01-01 至 2025-06-30。NASDAQ 100 持有 20 只，每笔成本 0.1%。
- 附录 D.1 称 CSI 300 成本为 0.5%，与 C.4.2 的 0.05%/0.15% 不一致（原文前后矛盾）。

**防过拟合与防前视（事实 [*]）**
- 论文附录 F.1 称 Co-STEER 有健康检查，会核验因子无泄露、非平凡，并在训练窗口内有统计意义。
- 但开源代码中的因子编码提示词只有一句提醒，要求处理数据时避免时间泄露（`rdagent/components/coder/factor_coder/prompts.yaml`）[RD-REPO*]，没有找到程序化的前视检查。
- **测试段参与反馈（源码事实 [*]）**：
  - Qlib 模板 `rdagent/scenarios/qlib/experiment/factor_template/conf_baseline.yaml` 的 `port_analysis_config.backtest` 从 `test_start` 开始，默认 2017-01-01；记录器挂了 `SignalRecord`、`SigAnaRecord`、`PortAnaRecord` [RD-REPO*]。
  - Qlib 中 `SignalRecord` 调用 `model.predict(dataset)`，而 `LGBModel.predict` 的 `segment` 参数默认为 `"test"`；`generate_label` 也固定取 `segments="test"` [QLIB*]。
  - `rdagent/scenarios/qlib/developer/feedback.py` 把 `IC`、`1day.excess_return_with_cost.annualized_return`、`1day.excess_return_with_cost.max_drawdown` 定为 `IMPORTANT_METRICS`，用当前结果与 SOTA 结果对比后生成反馈 [RD-REPO*]。
  - 默认切分写在 `rdagent/app/qlib_rd_loop/conf.py`：训练至 2014-12-31，验证 2015–2016，测试 2017-01-01 至 2020-08-01 [RD-REPO*]。
  - 结论：**按默认配置，LLM 每轮看到的、用来判断是否超越 SOTA 的指标，就是最终报告所用测试段上的指标。**论文实验是否改过这套配置，论文没有说明（**未核实**）。

**知识截止日（事实 [*]）**
- 主实验模型为 GPT-4o 和 o3-mini（表 1），官方截止日都是 2023-10-01 [OAI-4O*][OAI-O3M*]。测试期 2017–2020 **完全在截止日之前**。
- 补充实验（§4 末段、附录 D.1）的说法：
  - 作者称，为回应关于适用范围、时效性和数据泄露的质疑，换到 2024–2025 测试，两个 LLM 后端的截止日“完全或几乎”早于测试期。
  - 作者称框架以数据为中心，LLM 不接触原始行情和具体时间切分，只看到字段层面的说明，因此拿不到精确的时间边界或切分信息，从而降低泄露风险。
  - 附录列出 GPT-4o 截止 2023-10-01、o4-mini 截止 2024-06-01，后者“几乎全部”早于测试期。官方页核对一致 [OAI-O4M*]。
  - 据此，作者结论是框架“不受知识截止问题影响”。

**判断**
- 补充实验是本文样本中最认真对待截止日的一次。但按 Talaria 的 ADR-0009 口径（截止日 + 30 天之后才算有效），o4-mini 的结果有约 6 个月落在缓冲期内，不能全部算有效证据。
- “LLM 只见字段说明”能防止背诵具体行情，防不住“哪类因子近年有效”这种先验知识。
- 如果论文实验沿用了默认模板，测试段同时用于反馈和报告，主表数字是选择后的样本内结果。这一点比截止日问题更严重。
- Pan 等（2026）独立运行官方包时，在 57 个准入因子中查出 31.6% 存在前视或跨股票排名混用（§10.1），说明健康检查并没有程序化落实。

**开源与复用（事实 [*]）**：MIT 许可。README 写明**目前只支持 Linux**，大多数场景要求 Docker，且当前用户能免 sudo 运行；LLM 后端默认走 LiteLLM，可接多家提供商 [RD-REPO*]。量化场景绑定 Qlib 与 Qlib 格式的中国市场数据（`provider_uri: ~/.qlib/qlib_data/cn_data`）。

---

## 6. LLM + MCTS 公式挖掘（“Navigating the Alpha Jungle”）

来源：arXiv 2505.11122v3（2025-11-12）[AJ*]。

**做法（事实 [*]）**
- LLM 产出**公式表达式**。用 MCTS 管理搜索树：选中节点后，LLM 针对某一维度改写公式，生成子节点。
- 评分维度为有效性、多样性、换手、稳定性和过拟合风险；前四项按在有效因子库中的百分位打分，过拟合风险由 LLM 定性评分（§3、附录 K）。
- **频繁子树回避（FSA）**：从有效因子库中挖出最常见的子树（默认 3 个），要求 LLM 生成时避开它们（§3、附录 G）。
- 有效因子进入 Alpha Zoo，准入门槛随因子库变大而逐步抬高（§3）。

**评估（事实 [*]）**
- A 股 CSI 300 和 CSI 1000，预测 10 日和 30 日收益。训练 2011-01-01 至 2020-12-31，测试 2021-01-01 至 2024-11-30（§4）。
- 下游 LightGBM 在整个训练期上训练，不另设验证集；MLP 用 2020 年作早停验证集（附录 G）。
- 组合构建：top-k（股票池的 10%），每日最多换 n=k/w 只；**每笔成本 0.15%**（附录 G）。
- 美股补充实验：S&P 500，训练 2007–2015，测试 2016-01-01 至 2020-10-10（附录 H）。

**防过拟合与防前视（事实 [*]）**：有 FSA 和 LLM 过拟合风险评分；没有统计检验，也没有多重检验校正。

**知识截止日（事实 [*]）**
- 主模型为 GPT-4.1（§4），官方截止日 2024-06-01 [OAI-41*]。测试期 2021-01 至 2024-11，约 41/47 个月在截止日之前；美股补充实验的测试期完全在截止日之前。
- 附录 H“Investigation of Potential Data Leakage in LLMs”的说法：作者担心 LLM 训练数据里含有历史上表现好的因子公式，生成时可能只是回忆出来。于是让 GPT-4.1、Gemini 2.0 flash-lite、DeepSeek-v3 直接写“预期在 CSI 300 上 10 日收益表现好的公式”，各 10 个；结果与随机公式无显著差异，远低于本方法（表 5）。作者据此认为所测 LLM 没有利用泄露的优质公式。

**判断**：这个探针只检验了“直接背诵现成好公式”这一种泄露。搜索过程中，LLM 会依据测试期之前的回测反馈改写公式，而它对 2021–2024 年 A 股哪些风格有效的先验知识，可能影响改写方向。直接索要高收益公式不会触发这种影响，因此探针结果不能排除它。另外，测试期大部分在模型知识期之内，这一点没有回应。

**开源（事实 [*]）**：正文和附录未见代码仓库链接（只检索了 `github`、`code is available` 等关键词）。

---

## 7. QuantaAlpha（上海财大等，2026）

来源：arXiv 2602.07085v3（2026-05-18）[QA*]；仓库 `QuantaAlpha/QuantaAlpha` [QA-REPO*]。

**做法（事实 [*]）**
- 把一次端到端挖掘（假设 → 公式 → 代码 → 回测）看作一条轨迹 τ，终端奖励为 R(τ) = L(f(X), y) − λR(f)（式 2）。
- **变异**：自我反思定位到出问题的步骤，只重写该步骤之后的部分。**交叉**：把多条高奖励父轨迹中的优质片段拼成新轨迹（§4.2）。
- 生成阶段要求假设、公式、代码三者语义一致，并限制复杂度和 AST 冗余。之后在 **2021 年验证集**上按输出相关性去重：两个因子相关性超过阈值时，丢弃 RankIC 较低的一个（§4.1.2）。

**评估（事实 [*]）**
- CSI 300：训练 2016-01-01 至 2020-12-31，验证 2021 全年，测试 2022-01-01 至 2025-12-26（§5.1）。
- 每个 LLM 方法提交约 150 个因子，统一输入 LightGBM（§5.1）。
- 成本：买入 0.05%，卖出 0.15%（附录，Table 8 一带）。正文仍说明，实际部署需要补充交易成本建模、风控和实盘验证（§5.2）。
- 结果：GPT-5.2 版 IC 0.0472、ARR 4.68%、MDD 11.8%（摘要、§5.2）。迁移到 CSI 500 和 S&P 500，4 年累计超额收益分别约 40.28% 和 19.1%（摘要）。

**防过拟合与防前视（事实 [*]）**：有复杂度、冗余和验证集去重；没有统计检验。奖励 R(τ) 在哪一段数据上计算，正文没有写清（**未核实**）。

**知识截止日（事实 [*]）**
- 主模型 GPT-5.2，官方截止日 2025-08-31 [OAI-52*]。测试期约 44/48 个月在截止日之前。
- 对照模型为 Claude-4.5-Sonnet、DeepSeek-V3.2、Qwen3-235B（表 1），**它们的截止日本文未核实**。
- 全文检索无截止日和泄露讨论。

**判断**：用 2025-08 截止的模型测 2022–2025，是本文样本中重叠最严重的一例；而 ARR 只有 4.68%，属于很容易被泄露解释掉的量级。Pan 等（2026）的独立复现发现，其开源代码实现的入库规则比论文写的弱（§10.1）。

**开源（事实 [*]）**：README 徽章标 MIT，但**根目录没有 LICENSE 文件**，GitHub API 返回许可证为空 [QA-REPO*]。绑定 Qlib；数据发布在 Hugging Face 数据集 `QuantaAlpha/qlib_csi300`（A 股 2016–2025）。

---

## 8. Automate Strategy Finding with LLM in Quant Investment（Kou 等，港科大等）

来源：arXiv 2409.06289v4（2025-11-03）[KOU*]。

**做法（事实 [*]）**
- GPT-4o 从 11 篇因子研究文档中抽取并分类，得到 9 类共 100 个**种子因子**（§3.2）。
- 多模态多 agent 评估各因子的置信度和风险偏好，每类选出最优因子（式 7）。
- 最后按类别动态加权，合成为策略（式 8）。

**评估（事实 [*]）**
- 主实验：SSE 50，训练 2021-01 至 2022-06，验证 2022-07 至 2022-12，测试 2023-01 至 2024-01。累计收益 53.17%，同期指数 −13.22%；表中 Sharpe 写作 0.287（§4.4、表 4）。
- 跨期实验：CSI 300 和 S&P 500，三组滚动窗口，测试期分别为 2021、2022、2023 年的上半年（表 5、表 6）。
- 成本：只提到用换手上限“控制交易成本”（附录 A.8），**没有写明费率**。

**知识截止日（事实 [*]）**：GPT-4o，截止 2023-10-01 [OAI-4O*]。所有测试期**都在截止日之前或与之重叠**。全文检索无相关讨论。

**判断**
- 附录给 LLM 的选因子提示中，出现了“提供测试集（2023 年前四个季度的表现）后，选出最后一个季度表现最好的因子”的写法（附录 A 选因子流程）。如果按字面理解，测试期信息进入了提示词（**需进一步核实**，原文措辞含糊）。
- 累计收益 53% 而 Sharpe 只有 0.287，自相矛盾；H1 2023 年化 192% 的数字也缺乏可信度。
- 结论：本文的数字不宜作为证据。

**开源（事实 [*]）**：仓库 `kouzhizhuo/Automate-Strategy-Finding-with-LLM-in-Quant-investment`，GitHub API 未返回许可证。

---

## 9. TradingAgents（Tauric Research / UCLA / MIT）

来源：arXiv 2412.20138v7（2025-06-03）[TA*]；仓库 `TauricResearch/TradingAgents`，提交 `9fa1196`（2026-11-04）[TA-REPO*]。

**做法（事实 [*]）**
- LLM **直接输出每日买/卖/持有决策**。
- 角色：基本面、情绪、新闻、技术四个分析师 → 多空研究员辩论 → 交易员 → 激进、中性、保守三个风控角色辩论 → 基金经理拍板（§3–§4）。
- 推理型模型（o1-preview）负责分析和决策，快速模型（gpt-4o / gpt-4o-mini）负责取数和摘要（§4.3）。

**评估（事实 [*]）**
- AAPL、NVDA、MSFT、META、GOOGL，**2024-01-01 至 2024-03-29，约 3 个月**（§5.1）。
- 指标：累计收益、年化收益、Sharpe、MDD；基线为买入持有、MACD、KDJ+RSI、ZMR、SMA。
- 作者在脚注中承认，Sharpe 高于他们预期的经验范围，并解释为该期间回撤很少；还说明只测 3 个月是因为每次决策要调用 11 次 LLM、20 多次工具，成本受限（§6 脚注）。
- **正文没有写交易成本。**

**防前视（事实 [*]）**
- 论文称 agent 只用每个交易日之前可得的数据，因此消除了前视偏差（§5.1）。
- 开源仓库的 CHANGELOG 显示，论文 v7 之后数据层前视缺陷被多次修复 [TA-REPO*]：
  - v0.2.3（2026-03-29）：修复当 `curr_date` 落在取数窗口中间时，回测取数会泄露未来数据（#475）。
  - v0.3.1（2026-07-05）：Alpha Vantage 基本面的前视过滤原本没有生效，此版修复。
  - v0.4.0（2026-08-31）：修复 FRED 宏观数据按“今天”取、社交情绪数据无日期过滤等问题。
  - 另一版把“当前日期”放到分析师提示词开头，使工具调用的日期以运行日期为锚，而不是模型训练截止日（#836）。
- 现版本的 `tradingagents/dataflows/date_window.py` 把这些规则集中起来：日期夹到交易日，新闻按半开窗口过滤，无日期条目只在窗口包含当前时刻时保留，历史运行不提供当前快照 [TA-REPO*]。

**知识截止日（事实 [*]）**：o1-preview、gpt-4o、gpt-4o-mini 的官方截止日都是 2023-10-01 [OAI-O1P*][OAI-4O*][OAI-4OM*]。测试期从截止日后约 3 个月开始，满足 30 天缓冲。论文没有讨论模型记忆，只讨论了数据层前视。

**判断**
- 窗口虽在截止日之后，但只有 5 只大盘科技股、60 个左右交易日、没有成本，统计上几乎没有说服力。
- 论文期代码的数据层后来被证实有前视缺陷；论文所用版本是否受影响，无法从论文判断（**未核实**）。
- Profit Mirage 的重测（§10.2）显示，在截止日之前和之后的两个相近市况窗口之间，TradingAgents 的 Sharpe 衰减 55.68%。

**开源与复用（事实 [*]）**
- Apache-2.0。默认数据源为 yfinance、SEC EDGAR、FRED（需 API key）、Polymarket，可配置 Alpha Vantage；LLM 提供商可换（`tradingagents/default_config.py`）。
- `tradingagents/backtest.py` 的文档字符串说明：它只评估决策质量，不是组合模拟器，不包含数量、成交价和现金账本。

---

## 10. 评估方法与泄露的专门研究（用于判断上面各项的可信度）

### 10.1 Agentic Empirical Asset Pricing / SEADS（Pan、Ding、Giesecke，arXiv 2609.00731v1，2026-09-01）[AEAP*]

**事实 [*]**
- **问题**：作者认为，现有 LLM 因子挖掘只回测“产出”（因子或交易），没有回测“产出它的发现系统”。单次运行的高 Sharpe 可能来自校准良好的准入门，也可能来自未察觉的泄露缺陷，或者恰好抽到了有利的样本内窗口，三者无法区分（§1）。
- **建议的评估方法**：在多个历史决策点重跑整个发现流程（rolling re-execution）（§1、§3）。
- **SEADS 的机制**：
  - 确定性的 AST 前视防火墙，共 7 类规则（表 5）。
  - 验证门（表 6）依次检查：重复指纹、自坍缩 0.95、覆盖率 0.5、Rank-IC t 统计量 ≥ max(3.0, Bonferroni 动态值)、10 年分段的状态一致性、新颖度（与已有因子最大相关 ≤0.7）、偏 IC、Fama–MacBeth、符号一致性。
  - 附录 J：样本外段在代码中被排除在所有准入决策之外。
- **对其他系统的独立复现**（附录 G）：
  - **RD-Agent(Q)**：基于官方安装包核对。其评估器没有时间泄露检查，只检查能否执行和近似重复。在一次 300 候选的匹配预算运行中，原生评估器准入 57 个，其中 18 个（31.6%）被防火墙标记为时间前视或跨股票排名混用。
  - **QuantaAlpha**：其开源代码实现的入库规则弱于论文所述。
- **结果**（§5）：在两个美股面板上，五个发现系统都明显高于“不做发现”的下限，Sharpe 0.82–0.96 对 0.62。SEADS 名义第一（0.96±0.04），但与 Beyond Prompting（0.91）、QuantaAlpha（0.90）的差异在重复实验噪声内；与 RD-Agent(Q)、AlphaAgent（各 0.82±0.03）区分较明显。
- **自认的局限**（§6）：
  - 骨干模型泄露“是软的，而非没有”：所有角色都用同一个当前模型 GPT-5.2，而不是按历史日期选用当时发布的模型。
  - 从特征说明而非实际收益出发提出假设，能让泄露接近安全一端，但仍残留“哪些因子家族有效”的一般知识。滚动协议有多个决策日，这一残留比单一截止日的静态比较更严重。
  - 若改用按发布日期选择的模型（如截止 2023-10 的 GPT-4o-mini），干净的样本外只剩约 25 个月，样本太短，而且会混入旧模型推理能力较弱的影响。

**判断**：这是本文样本中唯一同时做到四件事的工作：程序化前视防火墙、显著性门槛带多重检验修正、样本外在代码中隔离、对他人系统做独立审计。它对软泄露的分析与 Talaria ADR-0009 的立场一致。代码尚未公开（作者称录用后发布）。论文仍是预印本。

### 10.2 Profit Mirage（Li 等，arXiv 2510.07920v1，2025-10）[PM*]

**事实 [*]**
- 用 GPT-4o（截止 2023-10）重测 FinMem、FinAgent、QuantAgent、FinCON、TradingAgents。在 NASDAQ-100 上比较两个大盘收益相近的窗口：2021 年 Q2–Q3（截止前，大盘 +13.79%）和 2024 年 Q3–Q4（截止后，大盘 +13.35%）（§2.1.1）。
- Sharpe 衰减 51.48%（QuantAgent）至 62.23%（FinCON）；总收益衰减 50.18%（TradingAgents）至 71.85%（FinMem）；TradingAgents 的 Sharpe 衰减 55.68%（§2.1.3）。
- 作者提出 FinLake-Bench 和反事实扰动框架 FactFin。

**判断**：每个系统只比较了两个窗口，市场状态的差异不只体现在指数收益上，因此衰减幅度的精度有限。但方向一致，而且在所有被测系统上都出现，足以支持“截止日之前的回测收益不可信”这一结论。

### 10.3 记忆与时点一致模型 [MEM*][CHR*]

- **事实 [*]**：Lopez-Lira、Tang、Zhu（arXiv 2504.14765v2）发现 LLM 能精确回忆截止日之前的经济和金融数据；要求模型“遵守历史边界”的指令无法阻止达到回忆级别的准确率；遮蔽实体也会被模型从少量上下文中还原；截止日之后则观察不到回忆。记忆还会体现在嵌入中（摘要）。
- **事实 [*]**：ChronoBERT / ChronoGPT（arXiv 2502.21206）按时点训练，在新闻次日收益预测上 Sharpe 与更大的 Llama 相当，作者据此判断该用法下前视偏差不大（摘要）。细节见 [已有调研](./llm-trading-lookahead-contamination.md) 第 3 节。
- **判断**：这两篇针对“读文本 → 预测收益”。因子挖掘中的泄露路径不同，LLM 不需要回忆具体价格，只需要“知道”某类风格在某段时间有效。目前没有找到直接测量这类泄露的工作；SEADS 只是承认它存在（§10.1）。

---

## 11. 对 Talaria 的适用性

### 11.1 约束回顾（事实，来自 Talaria ADR）

- ADR-0010：指标与算子以 `foundation/operators` 的纯 Python `Decimal` 实现为唯一权威，新算子须附黄金向量。
- ADR-0019：组件只能经按 `as_of` 截断的 `ctx` 取数；带版本号的 JSON 规则只能调用已注册算子；agent 写的 Python 须人工批准与内容哈希绑定的版本，并在受限子进程中运行。
- ADR-0007：Optuna 寻优前必须先通过截断式前视审计和预热漂移审计；切分为训练 / 验证（含 embargo）/ 物理封存的 Holdout；累计记录 `n_trials`；Holdout 在接口层不可见。
- ADR-0009：凡评估 agent 表现，与“模型截止日 + 30 天”有重叠的回放一律只算 `DIAGNOSTIC`；晋升只认 `POST_CUTOFF_REPLAY` 和 `SHADOW_FORWARD`。

### 11.2 三种形态的可行性（判断）

| 形态 | 与约束的契合 | 主要障碍 | 结论 |
|---|---|---|---|
| **A. LLM 离线生成公式 → 固化为确定性算子组合**（JSON 规则或注册算子树） | 最高。固化后的产物是普通组件：走 `ctx` 截断、可做截断式前视审计、可进 Optuna 三段切分、可复现，不必在运行时调用 LLM。 | 公式本身确定，但“选出这个公式”的过程受模型先验影响（§6、§10.1 的软泄露）。验证段的回测反馈也会把信息带回 LLM，相当于在验证段上做了大量隐式试验。 | **最可行**，但证据等级要按“生成过程”而不是“公式”来定（见 11.4 第 4 条）。 |
| **B. LLM 定期给出判断，记录为时间序列**（如每日新闻情绪、市场状态标签） | 中等。记录下来的序列可以作为 `ctx` 中按 `as_of` 可见的数据列，下游组件仍是确定性的。 | 历史回填就是在截止日之前让 LLM“回看”，Glasserman & Lin、Lopez-Lira 等已证明这类回填有偏；只有前向记录的部分才算证据。模型升级后序列会断代。 | **可行，但只能从启用当天开始前向积累**；历史回填一律标 `DIAGNOSTIC`。可以把时点一致模型（ChronoGPT）作为可回填的下限基线（已有调研结论）。 |
| **C. LLM 直接做交易决策** | 最低。不可确定性复现，只能“录制后复现”；每次决策成本高。 | TradingAgents 的证据薄弱；Profit Mirage 显示截止日之后衰减超过 50%。 | **只作为 `SHADOW_FORWARD` 实验**，不进入寻优和晋升主路径。 |

### 11.3 可以复用的开源组件（事实 + 判断）

| 组件 | 许可证（事实 [*]） | 能复用什么（判断） | 不能直接用的原因（事实 / 判断） |
|---|---|---|---|
| RD-Agent（`microsoft/RD-Agent`） | MIT | 回路结构（假设 → 编码 → 运行 → 反馈 → SOTA 追踪）、Co-STEER 的“写代码 → 执行报错 → 修正”提示词、老虎机调度思路 | 仅 Linux、依赖 Docker；量化场景绑定 Qlib 及其中国数据格式；默认用 test 段指标做反馈（§5）。建议只借鉴结构，在 Talaria 外圈编排层重写，反馈只读训练和验证段。 |
| Qlib（`microsoft/qlib`） | MIT | 表达式算子清单（Alpha158 / Alpha360）可作为 LLM 可用算子词表的参照 | 浮点实现，与 ADR-0010 的 Decimal 权威冲突；只能参考，算子须在 `foundation/operators` 重写并附黄金向量。 |
| TradingAgents | Apache-2.0 | `dataflows/date_window.py` 的时点规则（夹日期、半开窗口、无日期条目处理）可对照 Talaria 新闻工具层；多角色图可作为形态 C 的实验骨架 | 数据源以 yfinance / Alpha Vantage 为主，不覆盖加密永续；`backtest.py` 明确不是组合模拟器。 |
| AlphaAgent、QuantaAlpha | **无 LICENSE 文件**（README 自称开源或标 MIT） | 只借鉴论文中的思路：AST 最大公共子树相似度、复杂度项（式 8）、轨迹变异与交叉 | 没有许可证文件，不能拷贝代码；QuantaAlpha 代码与论文规则不一致（§10.1）。 |
| LLM+MCTS（2505.11122） | 未见代码 | 频繁子树回避、多维评分后回传的设计 | 无代码。 |
| SEADS / AEAP（2609.00731） | 代码未公开 | 验证门顺序与阈值（表 6）、AST 前视防火墙的规则类别（表 5）、滚动重跑协议 | 代码称录用后公开；面板是美股月频特征，不是 K 线。 |
| Optuna | MIT（ADR-0007 已采用） | 可用作 LLM 提议之后的数值参数精调 | 不负责结构搜索。 |

### 11.4 需要补的机制（建议）

1. **公式 DSL 与注册算子白名单。** LLM 只能输出 JSON 规则或 AST，叶子是 `ctx` 字段，内部节点只能是已注册的 Decimal 算子。LLM 提议新算子时转为人工任务：写 Decimal 实现并补黄金向量，然后才可用。这条与 ADR-0010、0019 一致，也直接消除“LLM 写 Python 偷看未来”这一类风险。RD-Agent(Q) 有 31.6% 的准入因子含前视，主要原因正是任意代码缺少程序化检查（§10.1）。
2. **静态前视防火墙，作为截断式审计的前置。** 在 AST 上拒绝负向位移、全样本统计（全样本归一化、全区间排名）、跨时间截面混用等模式，参照 SEADS 表 5 的规则类别。截断式审计只能发现已经造成数值差异的前视，静态规则可以在运行前拦住。
3. **准入门与搜索台账。** 每个 LLM 提议都计入 `n_trials`，包括失败、被去重和被复杂度拒绝的。准入要求 Rank-IC t ≥ max(3, 多重检验修正值)、AST 相似度和输出相关性都低于阈值（参照 AlphaAgent 式 8、SEADS 表 6），并限制复杂度。**LLM 只能看到训练段和验证段的指标，Holdout 在接口层不可见**，避免 RD-Agent 默认配置的问题。
4. **把 ADR-0009 扩展到“发现过程”。** 一个由 LLM 生成的公式，即使数值计算完全确定，只要它的 Holdout 与生成模型的“截止日 + 30 天”重叠，就只算 `DIAGNOSTIC`。可落地的有效证据有两种：
   - (a) **前瞻冻结**：在 T 日用模型 M 生成并冻结公式（记录模型版本、提示哈希、生成日期、公式哈希），只在 T 之后的 Shadow Run 或后续回放中评估；
   - (b) **截止后封存**：Holdout 起点晚于 M 的截止日 + 30 天，而且生成阶段完全看不到这段数据。
   - 模型升级后，按 ADR-0009 现有规则自动降级。
5. **发现过程的重跑。** SEADS 的滚动重跑思路可以用来衡量“这套发现流程是否稳定”。但在 Talaria，只有在拿得到按发布日期区分的模型版本时，历史重跑才有意义；否则重跑结果全部是 `DIAGNOSTIC`。更现实的做法是定期（如每月）前向重跑并积累记录。
6. **加密永续的成本与市场差异。** 所读工作全部基于股票截面，成本只有 A 股双边费率（§0 第 7 条）。Talaria 的评估要计入手续费、资金费率和滑点（ADR-0009 已要求），并且要接受一个事实：永续品种少，截面因子的统计功效远低于 300–1000 只股票的股票池。时序类规则更适合加密，但以上论文都没有针对时序规则的验证经验可借。
7. **独立复核。** 至少保留一条“复现他人系统 / 复现自己旧结果”的检查路径。SEADS 正是通过这种复核才发现 RD-Agent(Q) 和 QuantaAlpha 的实现问题。

---

## 12. 未能核实与范围说明

- **未核实**：
  - text-davinci-002、Claude-4.5-Sonnet、DeepSeek-V3/V3.2/R1、Qwen3、Gemini 2.0 flash-lite 的官方知识截止日。
  - AlphaAgent 与 QuantaAlpha 的迭代反馈取自哪一段数据。
  - RD-Agent(Q) 论文实验是否沿用了默认的 test 段反馈配置。
  - TradingAgents 论文所用版本是否受到后来修复的数据前视缺陷影响。
  - Kou 等附录的选因子提示中“测试集”一词的确切含义。
- **线索中提到但不属于 LLM 因子挖掘的**：
  - **AlphaForge**（arXiv 2406.18394v5，AAAI 2025）是生成—预测神经网络加动态组合，不使用 LLM（事实 [*]，摘要与方法节）。AlphaAgent、LLM+MCTS 把它当基线。仓库 `DulyHao/AlphaForge` 的 GitHub API 未返回许可证。
  - **FinGPT**：官方 README 列出的是情感分析模型和 FinGPT-Forecaster（Llama2-7B LoRA，道指 30 成分股，数据集为 2023-05 至 2024-05），**没有找到 FinGPT 名下的因子挖掘工作** [FINGPT*]。
- **检索到但未深读的 2025–2026 工作**（只看到标题或引用条目，[摘要] 级别都没有达到）：Chain-of-Alpha（2508.06312，PDF 下载失败）、AlphaPROBE（2602.11917）、FactorMiner（2602.14670）、AlphaMemo（2606.20625）、AlphaDiverse（2609.29014）、AlphaRLM（2610.14959）、AlphaPilot（2610.15488）、Beyond Prompting（Huang & Fan 2026，SEADS 将其列为基线）。其中可能有对截止日处理更好的工作，需另行补读。

---

## 参考来源

论文（PDF 原文，`pdftotext` 抽取后核对）：

- [AGPT*] Wang 等，*Alpha-GPT: Human-AI Interactive Alpha Mining for Quantitative Investment*，arXiv 2308.00016v2。https://arxiv.org/abs/2308.00016
- [AGPT2*] Yuan 等，*Alpha-GPT 2.0: Human-in-the-Loop AI for Quantitative Investment*，arXiv 2402.09746v1。https://arxiv.org/abs/2402.09746
- [QAG*] Wang 等，*QuantAgent: Seeking Holy Grail in Trading by Self-Improving Large Language Model*，arXiv 2402.03755v1。https://arxiv.org/abs/2402.03755
- [FAMA*] Li 等，*Can Large Language Models Mine Interpretable Financial Factors More Effectively? A Neural-Symbolic Factor Mining Agent Model*，Findings of ACL 2024。https://aclanthology.org/2024.findings-acl.233
- [AA*] Tang 等，*AlphaAgent: LLM-Driven Alpha Mining with Regularized Exploration to Counteract Alpha Decay*，arXiv 2502.16789（KDD 2025）。https://arxiv.org/abs/2502.16789
- [RDQ*] Li 等，*R&D-Agent-Quant: A Multi-Agent Framework for Data-Centric Factors and Model Joint Optimization*，arXiv 2505.15155v2。https://arxiv.org/abs/2505.15155
- [AJ*] Shi 等，*Navigating the Alpha Jungle: An LLM-Powered MCTS Framework for Formulaic Factor Mining*，arXiv 2505.11122v3。https://arxiv.org/abs/2505.11122
- [QA*] Han 等，*QuantaAlpha: An Evolutionary Framework for LLM-Driven Alpha Mining*，arXiv 2602.07085v3。https://arxiv.org/abs/2602.07085
- [KOU*] Kou 等，*Automate Strategy Finding with LLM in Quant Investment*，arXiv 2409.06289v4。https://arxiv.org/abs/2409.06289
- [TA*] Xiao 等，*TradingAgents: Multi-Agents LLM Financial Trading Framework*，arXiv 2412.20138v7。https://arxiv.org/abs/2412.20138
- [AEAP*] Pan、Ding、Giesecke，*Agentic Empirical Asset Pricing: Methodological Foundations*，arXiv 2609.00731v1。https://arxiv.org/abs/2609.00731
- [PM*] Li 等，*Profit Mirage: Revisiting Information Leakage in LLM-based Financial Agents*，arXiv 2510.07920v1。https://arxiv.org/abs/2510.07920
- [MEM*] Lopez-Lira、Tang、Zhu，*The Memorization Problem: Can We Trust LLMs' Economic Forecasts?*，arXiv 2504.14765v2（读摘要）。https://arxiv.org/abs/2504.14765
- [CHR*] He、Lv、Manela、Wu，*Chronologically Consistent Large Language Models*，arXiv 2502.21206（读摘要）。https://arxiv.org/abs/2502.21206
- [GL] Glasserman、Lin，*Assessing Look-Ahead Bias in Stock Return Predictions Generated By GPT Sentiment Analysis*，arXiv 2309.17322v1。https://arxiv.org/abs/2309.17322（详见已有调研）
- AlphaForge：Shi 等，arXiv 2406.18394v5。https://arxiv.org/abs/2406.18394

仓库（`git clone --depth 1` 或 GitHub API，2026-11-21）：

- [RD-REPO*] https://github.com/microsoft/RD-Agent ，提交 `484776c`。文件：`LICENSE`、`README.md`、`rdagent/scenarios/qlib/experiment/factor_template/conf_baseline.yaml`、`rdagent/scenarios/qlib/developer/feedback.py`、`rdagent/app/qlib_rd_loop/conf.py`、`rdagent/components/coder/factor_coder/prompts.yaml`。
- [QLIB*] https://github.com/microsoft/qlib （main 分支）：`qlib/workflow/record_temp.py`（`SignalRecord.generate_label` 固定取 `segments="test"`）、`qlib/contrib/model/gbdt.py`（`predict` 的 `segment` 默认 `"test"`）。
- [TA-REPO*] https://github.com/TauricResearch/TradingAgents ，提交 `9fa1196`。文件：`LICENSE`、`CHANGELOG.md`、`tradingagents/default_config.py`、`tradingagents/dataflows/date_window.py`、`tradingagents/backtest.py`。
- [AA-REPO*] https://github.com/RndmVariableQ/AlphaAgent ：README 与根目录列表（无 LICENSE 文件）。
- [QA-REPO*] https://github.com/QuantaAlpha/QuantaAlpha ：README 与根目录列表（无 LICENSE 文件）。
- [FINGPT*] https://github.com/AI4Finance-Foundation/FinGPT ：README。

模型知识截止日（官方页面，2026-11-21 抓取）：

- [OAI-4O*] https://developers.openai.com/api/docs/models/gpt-4o ：Oct 01, 2023
- [OAI-4OM*] https://developers.openai.com/api/docs/models/gpt-4o-mini ：Oct 01, 2023
- [OAI-O1P*] https://developers.openai.com/api/docs/models/o1-preview ：Oct 01, 2023
- [OAI-O3M*] https://developers.openai.com/api/docs/models/o3-mini ：Oct 01, 2023
- [OAI-O4M*] https://developers.openai.com/api/docs/models/o4-mini ：Jun 01, 2024
- [OAI-41*] https://developers.openai.com/api/docs/models/gpt-4.1 ：Jun 01, 2024
- [OAI-52*] https://developers.openai.com/api/docs/models/gpt-5.2 ：Aug 31, 2025
- [OAI-35*] https://developers.openai.com/api/docs/models/gpt-3.5-turbo ：Sep 01, 2021
- [OAI-GPT4T*] https://developers.openai.com/api/docs/models/gpt-4-turbo-preview ：Dec 01, 2023
- [LLAMA3*] https://github.com/meta-llama/llama3/blob/main/MODEL_CARD.md ：8B 截止 2023 年 3 月，70B 截止 2023 年 12 月
