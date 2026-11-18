# LLM 交易评测的前视污染：前向基准、匿名化与时点一致模型

- 调研日期：2026-11-18。
- 调研问题：用历史回放评估 LLM 交易智能体时，模型预训练记忆可能包含回测区间的未来信息（行情、财报结果），收益会因此虚高。本文核实四项资料，判断它们能否支撑 Talaria 的评估方法：
  1. Nof1 Alpha Arena 的实际规则，以及 StockBench、LiveTradeBench、AI-Trader 的评测窗口与防污染手段；
  2. Glasserman & Lin (2023) 关于匿名化、前视偏差与 distraction effect 的结论；
  3. 时点一致训练的模型 TimeMachineGPT 与 ChronoGPT/ChronoBERT；
  4. FINSABER 的长周期结论，以及它如何处理幸存者偏差和前视偏差。
- 方法：只读一手来源，包括 arXiv 论文 PDF 原文（注明版本号）、官方网站、官方 GitHub 源码与 README（固定提交）、Hugging Face 官方组织页和模型卡。论文用 `pdftotext` 抽出全文后逐段核对。**没有运行任何基准，也没有复现收益数字。**文中引用的数字都出自作者报告，没有经过独立验证。
- 访问限制：本次调研环境无法直连 `nof1.ai`（TLS 连接被重置），Alpha Arena 部分改读 Internet Archive 对该官方页面的存档（2025-11-12 快照），内容仍是 Nof1 官方原文。
- 与已有调研的关系：Look-Ahead-Bench（alpha 衰减诊断及其 runner 的执行时序问题）已在[执行笔记](<./agentic-trading-execution-sources.md>)第 B 节分析，本文不重复。框架级综述见[系统综述](<./agentic-trading-systems-survey.md>)。

标注约定：**原文事实**是来源中能直接读到的内容；**作者主张**是作者的结论或解释，本文未独立验证；**推导/建议**是本文针对 Talaria 的推论。

## 结论摘要

1. **前向评测分两类，说服力不同。**Alpha Arena Season 1 是真钱实盘：每个模型 1 万美元，在 Hyperliquid 交易 6 个币种的永续合约，约每 2–3 分钟调用一次。它没有历史回测，靠“实时交易，不做回放”从结构上避开记忆污染，官方也承认样本小、周期短、缺乏统计严谨性 [A1]。LiveTradeBench 用实时行情做模拟撮合的前向评测（2025-08-18 至 2025-10-24，共 50 个交易日）[L1]。这两者能从结构上排除模型记忆。StockBench 是**事后回放**，只是把窗口放在“主流 LLM 知识截止之后”，没有逐个模型列出截止日 [S1]。
2. **AI-Trader（HKU）名不副实。**论文自称“live、data-uncontaminated”，报告窗口是 2025-10 至 2025-11 [T1]。但同期官方 README 的核心是“Historical Replay”，起止日可任意设置；Trade 工具只把成交写进本地 `position.jsonl`；搜索工具的日期过滤在日期无法解析时默认保留结果，相对日期也按当前时刻而非模拟时刻换算 [T2][T3][T4]。论文窗口晚于各模型发布，这一点基本成立；但它的代码不能把任意历史回放变成“无污染”。
3. **Glasserman & Lin 的结论得到证实，但有两点限定。**在 GPT-3.5 训练窗口内（2015-01 至 2021-09），匿名化标题的样本内收益**高于**原标题，作者据此推断 distraction effect 的影响大于 look-ahead bias，大公司尤其明显。样本外（2021-10 至 2022-12）两者差异不再显著，只有大公司仍有临界显著的匿名化优势 [G1]。
4. **时点一致模型已经可以下载，但规模小。**ChronoGPT 有 1.55B 参数，覆盖 1999–2024 共 26 个年度版本；ChronoBERT 有 149M 参数；另有 instruct 版本，全部用 MIT 许可发布在 Hugging Face。作者报告，在 2008-01 至 2023-07 的新闻次日收益预测中，ChronoGPT 的多空 Sharpe（4.92）与 Llama-3.1-8B（4.90）相当，并据此判断“前视偏差不大”，但这个判断只适用于“嵌入 + 回归”这类用法 [C1][C3][C4]。TimeMachineGPT 是 117M 参数的 GPT-2 small，有 2011–2022 共 12 个年度版本，权重和数据公开，**论文本身没有金融预测实验** [M1][M2]。
5. **FINSABER 证实先前研究报告的优势在更长周期、更广标的下消失。**在 2004–2024 年、S&P 500 历史成分股（含退市股）、滚动窗口的设置下，FinMem/FinAgent 不敌买入持有，alpha 也不显著（p > 0.34）[F1]。但它所说的“前视偏差处理”只是**数据时间对齐和选股去未来化**，作者明确说明**没有校正预训练带来的数据泄漏**（GPT-4o 可能见过回测数据）[F1]。
6. **对 Talaria 的推导：**截止日之前的回放只能当诊断，结果必须同时报告匿名化对照。晋升证据只认截止日之后的回放和 Shadow Run。要单独测量“截止前减截止后”的衰减，Look-Ahead-Bench 的 alpha_decay 思路可作参照（见第 6 节）。

## 1. Alpha Arena 与 2025 年后的前向/实盘 LLM 交易基准

### 1.1 Nof1 Alpha Arena Season 1

**原文事实**（全部出自 Nof1 技术博文 [A1]）：

| 项 | 原文内容 |
|---|---|
| 资金与场所 | “$10,000 each to trade on Hyperliquid, with zero human intervention” |
| 标的 | Hyperliquid 上的 6 个币种：BTC、ETH、SOL、BNB、DOGE、XRP，均为永续合约，允许杠杆 |
| 模型 | GPT-5、Gemini 2.5 Pro、Claude Sonnet 4.5、Grok 4、DeepSeek v3.1、Qwen3-Max。除 Qwen3-Max 外，推理强度都开到最高可配置档；不做任务特定微调 |
| 频率 | 每次推理调用约 2–3 分钟（“~2-3 mins”）；作者称为分钟到数小时的中低频交易（MLFT） |
| 输入 | 只给数值行情：当前与历史中间价、成交量、技术指标、持仓量（OI）、资金费率、账户状态与 Sharpe。**Season 1 不给新闻和叙事信息** |
| 动作空间 | buy to enter、sell to enter、hold、close；输出须附带数量、杠杆、理由、置信度和退出计划（止盈、止损、失效条件） |
| 编排 | 所有模型使用同一 system prompt 和 user prompt 模板；没有多智能体、没有工具调用、没有长对话历史 |
| 时间 | “Season 1 of Alpha Arena runs live through November 3, 2025, 5:00 p.m. ET”。原文没有写开始日期 |
| 是否回测 | 没有。原文写“this is live trading, not a replay or a paper exercise” |

**如何规避污染（原文事实与作者主张）：**博文没有专门讨论“训练截止日”。它的理由分两层。第一，静态基准会被吸收进训练语料，模型可以“通过直接记忆”得高分，所以要转向“真实世界、动态、竞争性环境”。第二，选择真钱实盘，是因为纸面交易暴露不出执行、逆向选择和问责问题 [A1]。实时交易本身让模型不可能预先见过结果，污染因此在结构上被排除；但博文没有把这一点表述为防污染措施。

**作者自述的局限（原文事实）：**作者承认存在 prompt bias、样本量有限、缺乏统计严谨性、评测周期短，并称早期的成功“may be the result of luck”，不打算凭一轮比赛评出“最佳交易模型” [A1]。

**推导：**原文没有给开始日期。示例 prompt 里写着“It has been 2399 minute since you started trading”，对应 2025-10-19 10:10 ET，倒推开始时间约在 2025-10-17 傍晚（ET）。这是根据示例推算的，不是官方日期。Season 2 及之后各赛季（包括媒体所说的美股赛季）本次**未能核实**，因为能访问到的官方存档里只有这一篇技术博文 [A1]。

### 1.2 StockBench（清华 / 北邮，arXiv 2510.02209 v2）

- **原文事实：**这是“back-trading”环境，即事后回放。标的是道琼斯工业指数中权重最高的 20 只股票，每天决策 buy/sell/hold；输入包括开盘价、基本面指标和新闻（每次检索保留前 5 篇）；数据保留时间戳以“prevent leakage of future information”。主窗口是 2025-03-03 至 2025-06-30，共 82 个交易日 [S1]。
- **关于训练截止日，原文写法：**选窗口的第一条原则是数据“must not have been exposed to the evaluated stock trading agents during their model training stages”。原文称该窗口“falls after the knowledge cutoff of mainstream LLMs, ensuring no data leakage”，并承诺持续更新窗口 [S1]。**原文没有逐个模型列出训练截止日，也没有逐个核对。**评测模型包括 Qwen3、DeepSeek、Kimi-K2、GLM-4.5、GPT-OSS、O3、Claude-4-Sonnet 等 [S1]。
- **前后不一致（原文事实）：**正文其他处把窗口写成“March 2025 to July 2025”，与“March 3 to June 30”不一致。鲁棒性实验又引入下跌窗口（2025-01 至 2025-04）和上涨窗口：正文写“May to August 2025”，图 4 标注却是“May-Aug 2024” [S1]。原文没有论证这些附加窗口是否在每个模型的训练截止日之后。
- **作者自述的局限（原文事实）：**每天只交易一次；不建模交易成本、滑点和流动性；四个月窗口可能错过完整的牛熊周期 [S1]。

### 1.3 LiveTradeBench（UIUC，arXiv 2511.03628 v1）

- **原文事实：**设计原则之一是实时流式接入行情和新闻，“eliminating dependence on offline backtesting and preventing information leakage” [L1]。评测在 2025-08-18 至 2025-10-24 之间实时进行，共 50 个交易日，覆盖 21 个 LLM，分美股和 Polymarket 两个市场 [L1]。
- **执行方式（原文事实）：**价格是真实的，但持仓转移由模拟函数完成。作者假设所选资产流动性高，智能体交易对价格影响可以忽略，因此认为模拟转移“closely aligned with the real world”。**这不是真钱下单。**局限一节明确说明不计手续费、买卖价差和流动性约束 [L1]。
- **与训练截止日的关系（推导）：**它是实时前向评测，评测时刻晚于所有被测模型的发布时刻，模型不可能记住结果。原文的论证依据是“live streaming”，不是逐模型比对截止日 [L1]。

### 1.4 AI-Trader（香港大学 HKUDS，arXiv 2512.10971 v1）

- **名字核对：**名字能查到，与“AI-Trader（HKU）”的描述一致。作者为 Tianyu Fan 等人，通讯作者 Chao Huang，单位 University of Hong Kong [T1]。
- **论文原文：**论文自称“the first fully-automated, live, and data-uncontaminated evaluation benchmark”，并称“operates in live markets with real economic consequences” [T1]。报告的实验窗口如下：美股（Nasdaq-100）和 A 股，2025-10-01 至 2025-11-07；加密货币（10 个 USDT 计价交易对），2025-11-01 至 2025-11-14。A 股和加密为日频，美股为小时频。被测模型有 6 个：DeepSeek-v3.1、MiniMax-M2、Claude-3.7-sonnet、GPT-5、Qwen3-max、Gemini-2.5-flash [T1]。
- **同期官方源码与 README**（固定在 2025-11-20 的提交 `c3cefa3`）：
  - README 把“Historical Replay Capability”列为核心特性，`init_date`/`end_date` 写着“Any start date”，示例配置包含 2024-01-01 至 2024-03-31。环境描述为“controlled environment with real market data and historical replay capabilities”。初始资金为 1 万美元、10 万人民币或 5 万 USDT [T2]。
  - `tool_trade.py` 的成交步骤只是把记录写入本地 `data/.../position/position.jsonl`，代码里没有券商下单 [T4]。这与论文“executes actual buy and sell orders”“real economic consequences”的说法不符。
  - `tool_jina_search.py` 按 `TODAY_DATE` 过滤搜索结果，但**日期无法解析时保留该结果**，而且“N days ago”这类相对日期按 `datetime.now()` 而不是模拟时刻换算 [T3]。在历史回放中，这两条都是潜在的未来信息泄漏路径。
- **评估（推导）：**论文的实验窗口晚于各被测模型的发布，可以看作前向窗口。但仓库的主要用法是“任意区间回放 + 工具层未来信息过滤”，这只能挡住**工具返回的数据**，挡不住**模型权重中的记忆**。用它在模型截止日之前回放，不能自称 data-uncontaminated。

### 1.5 小结

| 基准 | 评测方式 | 窗口与截止日的关系（原文写法） | 资金 |
|---|---|---|---|
| Alpha Arena S1 | 实时实盘 | 原文写“live trading, not a replay”，没有讨论截止日 | 真钱，每模型 1 万美元 [A1] |
| StockBench | 事后回放 | 写明在“主流 LLM 知识截止之后”，但没有逐模型核对；附加窗口未论证 | 模拟 [S1] |
| LiveTradeBench | 实时前向，模拟撮合 | 依据“live streaming”避免泄漏 | 模拟，不计成本 [L1] |
| AI-Trader | 论文称 live；代码以历史回放为主 | 论文窗口在模型发布之后；代码可回放任意区间 | 本地账本模拟 [T1][T2][T4] |

## 2. Glasserman & Lin (2023)：匿名化与 distraction effect

文献：Paul Glasserman、Caden Lin，“Assessing Look-Ahead Bias in Stock Return Predictions Generated By GPT Sentiment Analysis”，arXiv 2309.17322 v1，2023-09 [G1]。

**样本设定（原文事实）：**

- 模型为 GPT-3.5（ChatGPT 引擎），作者取其训练数据截至 2021-09。样本内为 2015-01 至 2021-09，样本外为 2021-10 至 2022-12 [G1]。
- 两套新闻标题来源。一套是自行抓取的标题：129,431 条，覆盖 6,723 家公司，市值中位数 25.9 亿美元。另一套是 Thomson Reuters 标题：181,908 条，覆盖 678 家 S&P 500 公司，市值中位数 567.4 亿美元。收益数据来自 CRSP [G1]。
- 策略沿用 Lopez-Lira & Tang (2023)：让模型对标题回答 YES/NO/UNKNOWN，再按次日收益构建多空组合。作者强调这些策略“not intended to be feasible”，并忽略交易成本和卖空约束 [G1]。
- 匿名化方法：把公司名、相关产品或服务名替换为随机字符串“Blahblahblah”。抓取集中有 100,401 条被替换，TR 集中有 180,237 条被替换 [G1]。

**两种偏差的定义（原文事实）：**look-ahead bias 指模型知道新闻之后发生了什么，它对收益的影响“should be positive”。distraction effect 指模型对公司的一般知识干扰了对文本情绪的判断，方向不确定，并且样本外同样存在 [G1]。

**结论核对：**

- **证实（原文事实）：**“In-sample … the anonymized headlines outperform, indicating that the distraction effect has a greater impact than look-ahead bias.” 该结论在两个新闻源上都成立，大公司尤其明显 [G1]。
- **推理链（作者主张）：**look-ahead bias 必为正，匿名化后收益反而更高，所以 distraction effect 必为负，且绝对值更大。作者进一步解释为 GPT-3.5 存在“overconfidence”：distraction 有害时造成的损失，大于它有益时错过的收益 [G1]。
- **限定 1（原文事实）：**样本外，原标题与匿名标题的差异“loses statistical significance”。作者据此认为，样本内观察到的 distraction effect 大部分来自对未来的知识 [G1]。
- **限定 2（原文事实）：**样本外只有大公司仍存在“borderline significant”的匿名化优势。作者据此主张匿名化在样本外部署中也可能有用 [G1]。

**推导：**这篇论文的结论是“看到公司名弊大于利”，**不是**“前视偏差可以忽略”。它测的是 GPT-3.5 对单条标题做情绪三分类，与 Talaria 中“智能体读多源信息、做出带仓位的决策”差距很大，只能借用其方法（原文与匿名对照），不能借用其效应的大小或方向。

## 3. 时点一致训练的模型

### 3.1 ChronoBERT / ChronoGPT（He, Lv, Manela, Wu，WUSTL）

文献：“Chronologically Consistent Large Language Models”，arXiv 2502.21206 v3（2025-07 版）[C1]；另有“Instruction Tuning Chronologically Consistent Language Models”，arXiv 2510.11677 v2 [C2]。

- **方法（原文事实）：**每个版本只用截至该时间点可获得的文本训练。1999 版先在 2000 年之前的高质量文本上多轮预训练，此后按年增量训练到 2024 年 [C1]。ChronoGPT 基于修改版 modded-nanoGPT（使用 RoPE、QK-Norm、FlexAttention 等），1999 版训练了 71B token，约 10 个 epoch。ChronoBERT 1999 版训练了 460B token，超过 70 个 epoch。作者通过 124M 到 1.5B 的缩放实验，最终选定 1.5B 规模 [C1]。
- **规模（原文事实）：**ChronoGPT 有 1,552M 参数，上下文 1,792 token。ChronoBERT 有 149M 参数，上下文 1,024 token。知识截止日分别为 1999 年到 2024 年各年的 12 月 [C1][C3]。
- **能力（作者报告）：**ChronoBERT 的 GLUE 成绩超过原版 BERT，ChronoGPT 的 HellaSwag 成绩超过 GPT-2 (124M)，两者都优于 StoriesLM、FinBERT 等无泄漏模型 [C1]。论文用“某年总统是谁”这类问题演示：截止日之前答对，截止日之后答不出 [C1]。
- **金融预测（作者报告）：**数据为 Dow Jones Newswire，取 t−1 日 16:00 到 t 日 16:00 ET 的公司新闻，预测 t+1 日收益。2007 年用作 burn-in，检验期为 2008-01 至 2023-07；组合按十分位多空构建，每日再平衡，等权 [C1]。多空组合的年化 Sharpe 如下：ChronoBERT（实时版本）4.80，ChronoGPT（实时版本）4.92，Llama-3.1-8B 4.90，GPT-2 XL 4.57，ModernBERT 4.60，BERT 4.18，FinBERT 3.86，StoriesLM 3.42。ChronoGPT 与 Llama 3.1 的 Sharpe 差异检验 p 值为 0.685 [C1]。
- **作者主张：**“lookahead bias is modest”，并且前视偏差“model- and application-specific”，因为上层的回归或预测模型可以弥补语言理解上的不足 [C1]。
- **开源（原文事实）：**Hugging Face 的 `manelalab` 组织下有 78 个模型仓库：`chrono-gpt-v1`、`chrono-bert-v1`、`chrono-gpt-instruct-v1` 各 26 个年度版本（1999–2024）。模型卡标注 MIT 许可，开发者与论文作者一致 [C3][C4]。instruct 论文称同时公开“fully open, fixed model weights”，并把这类模型定位为“a conservative lower bound on forecast accuracy” [C2]。
- **推导：**检验期 2008–2023 与 Llama 3.1 的截止日（2023-12）完全重叠，但 Llama 3.1 并没有显著胜出。这个结果只适用于“文本嵌入 + 收益回归”。生成式智能体直接下交易决策时，记忆的作用方式不同，不能照搬“前视偏差不大”的结论。此外，1.5B 规模的模型无法替代 Talaria 所用的前沿模型，只适合做**对照组或下限基线**，也就是衡量“剔除泄漏后还剩多少可预测性”。表中年化收益约 60%，是不计成本的学术多空组合，不能当作可实现收益。

### 3.2 TimeMachineGPT（TiMaGPT，Oxford / Manchester）

文献：Drinkall、Rahimikia、Pierrehumbert、Zohren，“Time Machine GPT”，arXiv 2404.18543 v1 [M1]。

- **方法（原文事实）：**按年训练“point-in-time”模型，强调“nonprognosticative”，即对未来的事实和语言变化保持无知。训练数据是 Wikipedia 按修订历史重建的各年 12 月 31 日版本，加上 WMT News，两者按 0.6:0.4（WMT News : Wikipedia）采样 [M1]。
- **规模（原文事实）：**GPT-2 small 架构，117M 参数；2011–2022 年每年一个版本 [M1]。
- **能力（作者报告）：**在常识基准上与 GPT-2、OPT-125m、GPT-Neo-125m 处于同一量级。作者用 COVID 等新词的困惑度说明：传统做法（在静态模型上继续训练时间切片数据）会在事件发生前就“认识”这些词，TiMaGPT 不会 [M1]。
- **金融预测：****论文没有金融或收益预测实验**，只在讨论部分提到这类模型可用于时间序列预测 [M1]。
- **开源（原文事实）：**Hugging Face 的 `Ti-Ma` 组织下有 `TiMaGPT2-2011` 至 `TiMaGPT2-2022` 共 12 个模型，以及对应的 Wikipedia 年度数据集 [M2]。
- **相关旁证（原文事实）：**ChronoGPT 论文提到，Rahimikia & Drinkall (2024) 另有一套基于时间戳金融文本训练的模型，报告 2017–2023 年多空 Sharpe 为 3.45，但这批模型“since retracted … from Hugging Face” [C1]。本次没有读到这篇原文，也没有核实这批模型的现状。

## 4. FINSABER（Edinburgh 等，KDD 2026）

文献：Li、Kim、Cucuringu、Ma，“Can LLM-based Financial Investing Strategies Outperform the Market in Long Run?”，arXiv 2505.07078 v6，KDD ’26 [F1]；代码仓库 `waylonli/FINSABER` [F2]。

**设定（原文事实）：**

- 框架分两步：先在每个滚动窗口里，从当时的 S&P 500 成分股（含退市股）中选股，再用择时策略（规则、ML、RL、LLM）做日频决策 [F1]。
- 评测时间为 2004–2024 年；数据涵盖价格、新闻和 10-K/10-Q 文件，时间范围 2000–2024 年 [F1]。
- 选股方式有 Random Five、Momentum、Volatility、FinCon Selection Agent 四种，在所有窗口中分别涉及 91、84、63、80 个不同标的 [F1]。
- LLM 策略为 FinMem 和 FinAgent，底座为 GPT-4o 或 GPT-4o-mini [F1]。

**结论核对：**

- **证实（原文事实）：**“previously reported LLM advantages deteriorate significantly under broader cross-section and over a longer-term evaluation.” 作者先把 FinMem/FinAgent 原文的 4 只股票（TSLA、NFLX、AMZN、MSFT）延长到 2004–2024 年，除 TSLA 外买入持有等策略都能持平或胜出。换成 Composite 设置后，两种 LLM 策略都跑输简单市场基线；配对检验显示买入持有显著更优；LLM 的 alpha 不显著，p 值均大于 0.34 [F1]。
- **市场状态分析（作者主张）：**LLM 策略在牛市中过于保守，跑输被动基准；在熊市中风控不足，损失偏大 [F1]。
- **幸存者偏差（原文事实）：**使用历史成分股名单并显式纳入退市股；作者为数据提供了开源替代版本 [F1]。
- **前视偏差（原文事实，需要区分）：**FINSABER 处理的是**数据层面**的前视，即“all data inputs are aligned with each backtest window using only information available prior to the start date”，以及不按全期结果选股 [F1]。对**模型记忆层面**的前视，作者明确写道：“our reported LLM performances do not adjust for potential data leakage: given the use of pretrained models like GPT-4o, the LLMs may have seen parts of the data during training”。作者的论证是：即使存在这种泄漏，LLM 仍然不敌传统策略 [F1]。
- **作者主张（未独立验证）：**结论部分写道，先前研究的收益“stemmed from survivorship and look-ahead biases rather than genuine market inefficiency” [F1]。正文实际控制的是幸存者偏差、数据窥探和数据时间对齐，**没有单独度量模型记忆带来的前视**，所以这句话中“look-ahead”的部分缺少直接证据。

**推导：**FINSABER 证明的是“窄样本、短窗口高估了 LLM”，不能说明“模型记忆的污染有多大”。它的选股去未来化、含退市股和滚动窗口做法，可以直接用作 Talaria 回放数据层的纪律。模型记忆这一层仍需第 6 节的截止日前后对照。

## 5. 核实结果一览

| 项 | 结果 | 与预期描述不符或需要补充的地方 |
|---|---|---|
| 1a Alpha Arena 规则 | 证实 | 原文没有写开始日期；没有专门讨论训练截止日，污染在结构上由实盘排除；Season 2 及之后未能核实 |
| 1b StockBench | 部分证实 | 窗口选在“主流 LLM 截止后”，但属于事后回放，没有逐模型核对；窗口日期前后不一致（6 月 30 日 / 7 月；附加窗口写成 2024 / 2025） |
| 1c LiveTradeBench | 证实 | 是实时前向评测，但撮合是模拟的，不计成本，不是真钱 |
| 1d AI-Trader（HKU） | 部分证实 | 论文称 live 且无污染；同期代码以任意区间历史回放为主，用本地账本模拟成交，搜索过滤会放行未知日期 |
| 2 Glasserman & Lin | 证实 | 样本外差异不显著，只有大公司临界显著；研究对象是 GPT-3.5 情绪三分类 |
| 3a ChronoGPT/ChronoBERT | 证实 | 权重以 MIT 许可开源；“前视偏差不大”只适用于嵌入加回归的用法 |
| 3b TimeMachineGPT | 部分证实 | 方法和开源属实；论文没有金融预测结果，规模只有 117M |
| 4 FINSABER | 部分证实 | 优势消失得到证实；“前视”只处理到数据层，模型记忆泄漏被明确列为未校正 |

## 6. 对 Talaria 智能体回测评估的启示

以下全部是**推导/建议**，依据见对应引用。

1. **以模型训练截止日为界，把每次回放标成“诊断”或“证据”。**
   - 每次评估都记录所用模型的**官方训练数据截止日**和回放区间。区间与截止日有任何重叠，结果一律标为 `diagnostic`，不能作为策略或提示词晋升的依据。依据：Glasserman & Lin 样本内外的差异 [G1]；FINSABER 承认未校正泄漏 [F1]；Look-Ahead-Bench 的跨期衰减（见[执行笔记](<./agentic-trading-execution-sources.md>)）。
   - 不要把“给 prompt 注入模拟日期”或“工具层按时间过滤”当作无污染证明。AI-Trader 的工具层过滤本身就有放行路径，而且根本触及不到模型权重 [T2][T3]。

2. **截止日之前的回放必须同时报告匿名化对照。**
   - 美股部分：用占位符替换公司名、代码、产品名和高管名（沿用 Glasserman & Lin 的方法），价格改成相对收益或按标的归一，日期改成相对日期。原始版与匿名版两条曲线并列展示。
   - 解读时注意方向不定：匿名版更好可能是 distraction，更差可能是记忆前视，也可能只是信息丢失。差值只能说明“模型依赖身份信息”，不能当作纯粹的前视偏差估计 [G1]。
   - 加密永续部分：匿名化价值有限，因为 BTC 和 ETH 的价格轨迹本身就可识别。截止日前的加密回放只用来查执行、风控和格式类缺陷，不用来评估 alpha。这一点是推导，没有文献直接检验加密场景。

3. **晋升证据只认两类来源。**
   - **截止日之后的回放：**区间严格晚于所有参与模型的截止日，并留出缓冲期（建议至少 1 个月，因为公开截止日通常是近似值）。数据层同时满足 FINSABER 的纪律：成分股按时点确定、含退市股、滚动窗口、不按全期结果选股 [F1]。
   - **Shadow Run（实时前向）：**仿照 LiveTradeBench 和 Alpha Arena，用实时数据驱动、事后不改 [L1][A1]。Talaria 要比 LiveTradeBench 更进一步，计入手续费、资金费率和滑点，因为 LiveTradeBench 在局限一节自己承认不计这些 [L1]。
   - 两类证据都要报告样本长度和统计不确定性。Alpha Arena 自己就承认单轮短窗口可能只是运气 [A1]；FINSABER 显示短窗口、窄标的的结论会在长周期下反转 [F1]。

4. **衰减测量作为常规指标。**
   - 对同一套智能体配置，定义 `decay = 指标(截止后窗口) − 指标(截止前窗口)`，指标可选超额收益、Sharpe 或命中率；同时报告匿名化前后的差值。截止前明显好于截止后，记作记忆污染或过拟合的信号。思路同 Look-Ahead-Bench 的 alpha_decay，其实现问题见[执行笔记](<./agentic-trading-execution-sources.md>)。
   - 注意市场状态的混淆：StockBench 显示下跌和上涨窗口的模型排名会大幅变化 [S1]，FINSABER 也显示 LLM 在牛熊市中的表现明显不同 [F1]。衰减值应在相近的市场状态之间比较，或者至少标注各窗口所处的市场状态。
   - 可选的下限基线：用对应年度的 ChronoGPT 或 ChronoBERT 生成新闻特征，作为“无泄漏可预测性”的参照 [C1][C3]。只用于美股新闻特征，不替代前沿模型做决策。

5. **登记基准的口径。**在 GLOSSARY 或评估元数据中区分“实盘前向”“模拟前向”“截止后回放”“截止前回放（诊断）”四类。引用外部基准时按上面第 1 节小结表的分类写，不沿用基准自称的“live”或“contamination-free”。

## 参考来源

[A1]: https://nof1.ai/blog/TechPost1
[S1]: https://arxiv.org/abs/2510.02209v2
[L1]: https://arxiv.org/abs/2511.03628v1
[T1]: https://arxiv.org/abs/2512.10971v1
[T2]: https://github.com/HKUDS/AI-Trader/blob/c3cefa31ae85727746cd65296d276f4ff81fb062/README.md
[T3]: https://github.com/HKUDS/AI-Trader/blob/c3cefa31ae85727746cd65296d276f4ff81fb062/agent_tools/tool_jina_search.py#L23-L98
[T4]: https://github.com/HKUDS/AI-Trader/blob/c3cefa31ae85727746cd65296d276f4ff81fb062/agent_tools/tool_trade.py#L198-L215
[G1]: https://arxiv.org/abs/2309.17322v1
[C1]: https://arxiv.org/abs/2502.21206v3
[C2]: https://arxiv.org/abs/2510.11677
[C3]: https://huggingface.co/manelalab/chrono-gpt-v1-20241231
[C4]: https://huggingface.co/manelalab
[M1]: https://arxiv.org/abs/2404.18543v1
[M2]: https://huggingface.co/Ti-Ma
[F1]: https://arxiv.org/abs/2505.07078v6
[F2]: https://github.com/waylonli/FINSABER

- [A1] Nof1，“Exploring the Limits of Large Language Models as Quant Traders”（Alpha Arena Season 1 技术博文）：https://nof1.ai/blog/TechPost1 ；本次读取的是 Internet Archive 存档：https://web.archive.org/web/20251112224723/https://nof1.ai/blog/TechPost1
- [S1] Chen et al.，StockBench，arXiv 2510.02209 v2（2026-03-02）：https://arxiv.org/abs/2510.02209v2 ；项目页 https://stockbench.github.io/
- [L1] Yu, Li, You，LiveTradeBench，arXiv 2511.03628 v1（2025-11-05）：https://arxiv.org/abs/2511.03628v1 ；仓库 https://github.com/ulab-uiuc/live-trade-bench
- [T1] Fan et al.，AI-Trader，arXiv 2512.10971 v1（2025-12-01）：https://arxiv.org/abs/2512.10971v1
- [T2] HKUDS/AI-Trader README（提交 `c3cefa3`，2025-11-20）：https://github.com/HKUDS/AI-Trader/blob/c3cefa31ae85727746cd65296d276f4ff81fb062/README.md
- [T3] 搜索工具：相对日期按 `datetime.now()` 换算（L37–L54）；日期无法解析时保留结果，只在 `TODAY_DATE > 发布时间` 时放行（约 L185–L197）：https://github.com/HKUDS/AI-Trader/blob/c3cefa31ae85727746cd65296d276f4ff81fb062/agent_tools/tool_jina_search.py
- [T4] 交易工具：把成交写入本地 `position.jsonl`：https://github.com/HKUDS/AI-Trader/blob/c3cefa31ae85727746cd65296d276f4ff81fb062/agent_tools/tool_trade.py
- [G1] Glasserman & Lin，arXiv 2309.17322 v1（2023-09-29）：https://arxiv.org/abs/2309.17322v1
- [C1] He, Lv, Manela, Wu，“Chronologically Consistent Large Language Models”，arXiv 2502.21206 v3（2025-07-06）：https://arxiv.org/abs/2502.21206v3
- [C2] He, Lv, Manela, Wu，“Instruction Tuning Chronologically Consistent Language Models”，arXiv 2510.11677：https://arxiv.org/abs/2510.11677
- [C3] ChronoGPT 模型卡（2024 版，MIT）：https://huggingface.co/manelalab/chrono-gpt-v1-20241231
- [C4] Hugging Face `manelalab` 组织（共 78 个模型仓库）：https://huggingface.co/manelalab
- [M1] Drinkall et al.，“Time Machine GPT”，arXiv 2404.18543 v1（2024-04-29）：https://arxiv.org/abs/2404.18543v1
- [M2] Hugging Face `Ti-Ma` 组织（TiMaGPT2-2011 至 TiMaGPT2-2022 及数据集）：https://huggingface.co/Ti-Ma
- [F1] Li, Kim, Cucuringu, Ma，FINSABER 论文，arXiv 2505.07078 v6（2026-06-26，KDD ’26）：https://arxiv.org/abs/2505.07078v6
- [F2] FINSABER 代码与数据：https://github.com/waylonli/FINSABER
