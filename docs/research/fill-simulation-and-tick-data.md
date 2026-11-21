# 成交模拟的数据粒度与高频原始数据（盘口、逐笔）的获取和存储

- 调研日期：2026-11-20。只使用官方文档、官方源码（固定提交）和交易所官方数据站点；没有登录，没有使用 API Key，没有调用收费接口。
- 范围：对应 [platform-redesign 需求](<../../.scratch/platform-redesign/spec.md>) D2 之后留给 D3 的问题：模拟成交模块放在哪一侧；用户决定纳入数据接口决策的“盘口与逐笔成交的存储”。具体是四个问题：① NautilusTrader、LEAN、Hummingbot 如何用 K 线、买一卖一、逐笔、多档盘口做成交模拟，只有 1m K 线时官方承认哪些局限；② 模拟成交与策略、账本是否同进程，模拟盘如何复用回测的成交模块；③ Bybit（为主）、Binance、Alpaca 的高频原始数据如何获取；④ 存储量级与布局。
- 方法：
  - 源码：沿用 [unified-strategy-runtime-sources.md](<./unified-strategy-runtime-sources.md>) 的三个固定提交，用 `git show <commit>:<path>` 阅读：
    - NautilusTrader `develop` [`0db3667a3c7b13e01f12a9bf446616c571f1fce0`](https://github.com/nautechsystems/nautilus_trader/tree/0db3667a3c7b13e01f12a9bf446616c571f1fce0)（开发分支，不代表已发布稳定版）；
    - QuantConnect LEAN [`53236fa4a23433a96c7f57cbe7850dba9be840ac`](https://github.com/QuantConnect/Lean/tree/53236fa4a23433a96c7f57cbe7850dba9be840ac)；
    - Hummingbot [`3351adad0a09f6a57a202219d3e52fe794672421`](https://github.com/hummingbot/hummingbot/tree/3351adad0a09f6a57a202219d3e52fe794672421)。
  - 在线文档：WebFetch 读 Bybit V5、Alpaca 官方文档；Bybit 的历史数据下载页（`www.bybit.com/.../history-data`）两次超时、curl 只拿到跳回首页的结果，没能读到内容。
  - 实测：从 `public.bybit.com`（Bybit 官方归档站，Bybit 文档 recent-trade 页指向“从网站下载归档逐笔”）和 `data.binance.vision`（Binance 官方，SHA-256 校验文件一致）各下载 2026-11-19 的 BTCUSDT 永续日文件到 `mktemp -d` 建的系统临时目录；用 pyarrow 26.0.1 转 Parquet 测体积；另用公共 WebSocket（免密钥）订阅 BTCUSDT 5 分钟测推送频率。脚本放在会话临时目录，没有进仓库。
- 标注约定：**事实**是源码或文档直接支持的内容；**作者宣称**是官方定性说法；**实测**是本次下载或订阅测得的数字（单日、单时段，不代表常态）；**推断**是面向 Talaria 的估算或建议。

不重复的已有结论（只链接）：

- Nautilus 的执行面（RiskEngine、对账、单线程内核）见 [agentic-trading-execution-sources.md §3](<./agentic-trading-execution-sources.md>)。
- 三个项目的进程模型（策略与数据引擎同进程、LEAN `live-paper` = 实时数据 + `PaperBrokerage`、Nautilus Sandbox 环境、外部 MessageBus 丢弃语义）见 [unified-strategy-runtime-sources.md §2–4](<./unified-strategy-runtime-sources.md>)。本文 §3 只补“成交模块”这一块。
- Bybit/Binance/Hyperliquid 的 K 线、OI、资金费历史深度与限频，Binance USD-M `bookTicker` 归档 2024-03-30 后停更，Alpaca IEX/SIP 基本限制，见 [perp-and-calendar-data-sources.md §2–3](<./perp-and-calendar-data-sources.md>)。
- 日内永续 agent 框架综述见 [intraday-perp-agentic-frameworks-survey.md](<./intraday-perp-agentic-frameworks-survey.md>)。

## 1. 结论先行

1. **只有 1m K 线时，三个项目都只能“编一条 K 线内路径”，并且都在文档或源码里承认这一点。**Nautilus 把每根 K 线拆成 O→H→L→C 四个合成 L1 更新逐点撮合，默认先高后低，可选“离开盘价近的极值先到”的启发式，官方明说这是确定性启发式、不是还原真实成交顺序，并建议依赖点差、K 线内顺序、紧止损或排队位置的策略改用 quote/trade/depth 数据验证。[NT-bar]、[NT-data] LEAN 的注释写明“用 OHLC 无法很好地模拟限价单”，按“流动、高成交量市场”假设整笔成交，不看成交量。[L-fm]、[L-efm] Hummingbot 回测只用 K 线（默认 1m），入场按收盘价，同根 K 线同时触及止盈和止损时**按止盈结算**（与 Talaria 法则 ③ 相反）。[H-pos]
2. **数据粒度决定能模拟到哪一层，这在 Nautilus 里是硬约束。**L1 撮合用 bar / quote / trade；L2/L3 撮合只认 `OrderBookDelta(s)`/`OrderBookDepth`，quote 和 bar 不更新 L2 簿；Nautilus 不能从低粒度数据生成 L2/L3。排队位置（`queue_position`）需要 L2/L3 + 逐笔。[NT-data]、[NT-queue] 换句话说：只存 L1 → 能做点差和挂单穿价判断；加逐笔 → 能做“对手方真实成交才算挂单成交”和成交量上限；加多档 → 才能做冲击滑点和排队。
3. **三个项目的成交模拟都和策略、账本在同一进程；模拟盘复用回测撮合的程度不同。**Nautilus 的 Sandbox 执行客户端直接复用回测用的同一个 `OrderMatchingEngine`，配置项（book_type、fill/fee/latency 模型、`bar_adaptive_high_low_ordering`、队列位置）同名同义。[NT-sbx] LEAN 的 `PaperBrokerage` 继承 `BacktestingBrokerage`，用同一个 `security.FillModel`。[L-paper] Hummingbot 的 paper trade 是另一套代码（用实时订单簿吃单、挂单被盘口穿价或逐笔匹配），和回测的 K 线模拟器不是同一个模块。[H-paper]
4. **Bybit 历史只有逐笔是免费、可脚本下载的；盘口历史只在需要浏览器的下载页。**`public.bybit.com/trading/` 提供按日 `.csv.gz` 逐笔（BTCUSDT 从 2020-03-25 起，次日约 01:10 UTC 上传，实测）；目录里没有盘口。官方公告说期货盘口历史下载自 2025-08-21 起从 500 档@100ms 改为 200 档@100ms，入口是 `www.bybit.com/derivatives/en/history-data`，本次没能读取该页。[BB-pub]、[BB-ann] 实时侧 `orderbook.1` 每 10ms、只推快照；`orderbook.50` 20ms、快照+增量；`publicTrade` 实时、带 `seq`。[BB-ob]、[BB-trade]
5. **存储量级（实测，单日 2026-11-19）：Bybit BTCUSDT 永续 255 万笔/日；官方 gz 92 MB；转成精简 Parquet（zstd-1，Nautilus 默认参数）不带成交 ID 约 15 MB/日，带 UUID 成交 ID 约 66 MB/日。**成交 ID 占了体积的四分之三以上。按 5 分钟 WS 实测推送率推算（推断）：L1 约 15–50 MB/日，50 档增量约 100–300 MB/日，200 档约 170–520 MB/日；`orderbook.1` 在 BTCUSDT 上每条都变化（约 46 条/秒），“只在变化时落盘”几乎不省。

## 2. 成交模拟的数据粒度（问题 1）

### 2.1 NautilusTrader：按 `book_type` 选数据，K 线拆四点（事实）

数据粒度与撮合簿类型的关系 [NT-data]：

| 数据 | `L1_MBP` | `L2_MBP` | `L3_MBO` |
|---|---|---|---|
| `QuoteTick` | 更新簿 | 忽略 | 忽略 |
| `TradeTick` | 触发撮合 | 触发撮合 | 触发撮合 |
| `Bar` | 更新簿 | 忽略 | 忽略 |
| `OrderBookDelta(s)` | 忽略 | 更新簿 | 更新簿 |
| `OrderBookDepth` | 更新簿 | 更新簿 | 更新簿 |

- “Nautilus **cannot generate higher granularity data** (L2 or L3) from lower-level data such as quotes, trades, or bars.”[NT-data]
- 官方建议（作者宣称）：先用 K 线验证信号；结果依赖点差、K 线内精确顺序、紧出场或排队位置时，改用 quote、trade 或 depth 数据。[NT-data]

**K 线执行（bar execution）**[NT-bar]：

- 生效条件：venue `bar_execution=True`、`book_type=L1_MBP`、K 线来自外部聚合（内部由 tick 聚合的 K 线不驱动撮合）；L1 venue 上非时间类 K 线会让 `run()` 抛错。
- 每根 K 线：成交量四等分（余数给收盘），依次处理开盘、高/低（按配置顺序）、收盘四个合成更新，每个更新后撮合一次，**然后**才把整根 K 线交给策略。所以 K 线开始前已挂着的单可以在中间点成交；在 `on_bar` 里下的单要等这根 K 线的四个点都处理完。
- `ts_init` 必须是 K 线**收盘时刻**，否则整根 K 线会在形成前可见；开盘时间戳的数据要加一个周期。
- `bar_adaptive_high_low_ordering`：默认 `False` 一律 O→H→L→C；`True` 时离开盘价更近的极值先到。源码判据是 `!adaptive || high - open < open - low`（相等时先走低点）。[NT-bar-src] 文档原话称它为 “deterministic heuristic, not a reconstruction of the actual trade sequence”，引用的依据只是一份 EUR/USD 探索性分析，“does not establish a general accuracy rate”。
- 触发类市价单（`STOP_MARKET` 等）：K 线开盘已越过触发价 → 按开盘（模拟市价）成交，即跳空滑点；开盘未越过、K 线中途穿过 → 按触发价成交（假设连续行情）。[NT-fill]
- 没有原生“下一根开盘价成交”模式；在 `on_bar` 里用当前 K 线开盘价会引入前视。延迟模型（`latency_model`）让订单在到达时刻之后的第一根 K 线扫完四点再结算，即只有 K 线时会看到那根 K 线的收盘。[NT-bar]
- `prob_slippage`（L1 下每次成交按概率差一跳）和 `prob_fill_on_limit`（触价不穿价时按概率成交）默认 0 和 1；L2/L3 下滑点由簿本身决定。[NT-fm]
- L1 市价单吃完盘口显示量后，余量按差一跳成交（确定性规则，与概率滑点无关）。[NT-fill]

**逐笔驱动的撮合**[NT-trade]：

- 一笔 `SELL` 主动成交可以成交我方挂着的 BUY，反之亦然；无方向成交两边都算。成交量上限 `min(剩余量, 该笔成交量)`；`liquidity_consumption=True` 时同一笔成交量不会被多个模拟单重复使用。
- 官方提示：逐笔成交“proves that liquidity existed momentarily, not that it stayed available”。[NT-fill]
- **排队位置**（`queue_position=True` + `trade_execution=True`）：挂单时记录同价位已显示量，之后同侧逐笔在该价位的成交量逐步扣减，扣到 0 才可成交。需要 L2/L3 簿 + 逐笔。[NT-queue]

### 2.2 LEAN：取最新一条订阅数据的 OHLC，整笔成交（事实）

- 默认装配：股票用 `EquityFillModel`；`Crypto`、`CryptoFuture` 等用 `ImmediateFillModel`（即基类 `FillModel`）；默认滑点 `NullSlippageModel`。[L-dbm]
- 基类取价顺序 `GetPrices`：订阅了 Tick 就用最新 tick 的买一/卖一（卖单用 bid，买单用 ask），否则用 QuoteBar 的 Bid/Ask 侧 OHLC，否则用 TradeBar。[L-fm-prices]
- 基类市价单：用当前价（或挂单在本 K 线前时用开盘），加滑点模型给的价差；数据过旧时等新数据。整笔成交（“assume the order completely filled”）。[L-fm-market]
- 基类限价买单：`Low < limit` 即整笔成交，价格 `min(High, limit)`。止损卖单：`Low < stop` 即成交，价格 `min(stop, 当前价 - slip)`（“worse case scenario”）。[L-fm-limit]、[L-fm-stop]
- 源码注释（作者自述局限）：“There is no good way to model limit orders with OHLC because we never know whether the market has gapped past our fill price. We have to make the assumption of a fluid, high volume market.” 止损限价单也无法确定 H、L 的先后。[L-fm]
- `EquityFillModel` 对股票更细：止损按 TradeBar 低/高触发，开盘跳空越过时按开盘价成交；限价单**严格穿价**才成交（“we assume our order is the last in the book with its limit price”），有利跳空时按开盘价成交；仍整笔成交，源码留着 `TODO: Add separate DepthLimited fill partial order quantities based on tick quantity / bar.Volume`。[L-efm]
- 滑点模型：`ConstantSlippageModel`（固定百分比）、`VolumeShareSlippageModel`（`volumeLimit² × priceImpact`，以 TradeBar 成交量或 QuoteBar 的最新买一/卖一量为分母）、`MarketImpactSlippageModel`（Almgren 等 2005，注释承认默认参数是约 20 年前标定的）。[L-slip]
- 同一根 K 线里止盈止损都能触发时，LEAN 各订单独立判断，没有统一的“先止损”规则（推断：源码里各 `XxxFill` 只看自己订单）。

### 2.3 Hummingbot 回测：只用 K 线，向量化（事实）

- `run_backtesting(..., backtesting_resolution="1m", trade_cost=0.0002)`：先加载 K 线，再逐个执行器模拟；手续费是单一比例。[H-engine]
- `PositionExecutorSimulator`：限价入场以“收盘价 ≤ 入场价”判断并**按收盘价**成交；止盈按收盘价算出的累计收益判断；止损按 low/high 判断，开盘越过止损时按开盘价；入场那根 K 线的高低点不参与止损判断。[H-pos]
- 同一时间戳既满足止盈又满足止损时，代码先比较 `close_timestamp == first_tp_timestamp`，**按止盈结算**。[H-pos]（与 Talaria 法则 ③ “同根 K 线悲观止损优先”相反。）
- 没有找到 Hummingbot 对“只用 K 线回测”的官方局限说明。

### 2.4 对照 Talaria 六条法则（推断）

| 法则 | 1m K 线够不够 | 需要的最低数据 | 参照 |
|---|---|---|---|
| ① 决策延迟墙 | 够（粒度 = 1 分钟） | 秒/毫秒级延迟要 L1 或逐笔 | Nautilus latency model [NT-bar] |
| ② 挂吃单区分 | 只能“穿价才算 Maker 成交”，拿不到点差 | 吃单点差要 L1；挂单真实成交要逐笔；排队要 L2+逐笔 | [NT-trade]、[NT-queue] |
| ③ 同根悲观止损 | 只能靠规则 | 逐笔可还原真实先后 | Nautilus adaptive 只是启发式 [NT-bar] |
| ④ 跳空滑点 | 开盘越价可模拟 | 多档盘口才有冲击滑点 | [NT-fill]、[L-efm] |
| ⑤ Mark Price 强平与资金费 | 需要 mark price K 线 + 资金费历史（REST 可补） | — | Nautilus `MARK_PRICE` 触发只认 `MarkPriceUpdate` [NT-fill] |
| ⑥ 确定性 | 与粒度无关；概率模型要固定种子 | — | [NT-fm] |

## 3. 成交模拟模块放在哪个进程（问题 2，只补成交模块）

| 项目 | 回测撮合 | 模拟盘撮合 | 与策略/账本 | 来源 |
|---|---|---|---|---|
| Nautilus | `SimulatedExchange` 为每个标的建一个 `OrderMatchingEngine` | `SandboxExecutionClient` 自己持有按标的的 `OrderMatchingEngine` 表，喂实时 quote/trade/bar/depth | 同一进程（执行客户端是节点内组件；账户余额、持仓在客户端内跟踪） | [NT-exch]、[NT-sbx] |
| LEAN | `BacktestingBrokerage.Scan()` 调 `security.FillModel` | `PaperBrokerage : BacktestingBrokerage`，同一 FillModel | 同一进程 | [L-paper] |
| Hummingbot | `ExecutorSimulator`（DataFrame 向量化） | `PaperTradeExchange`（实时订单簿 `simulate_buy/sell`、盘口穿价或逐笔匹配挂单） | 同一进程；两套代码 | [H-pos]、[H-paper] |

补充事实：

- Nautilus Sandbox 的 `latency_model` 只延迟“进”方向（提交/改单/撤单），交易所事件不延迟；没有数据流时靠时钟提醒释放到期命令。[NT-sbx-lat] 回测侧则把命令放进 inflight 队列，只有**该标的**的行情或定时器能释放，别的标的行情不会让订单撞上过期盘口。[NT-bar]
- Nautilus Sandbox 文档：L2/L3 时每个标的只喂 delta 或只喂 depth 快照，不要混用；策略必须订阅逐笔（和 L2/L3 delta）才能用逐笔撮合。[NT-sbx]、[NT-queue]
- 三者都没有“策略进程把订单发到另一个进程里的模拟撮合”的默认形态。Talaria 的 D2 已定策略在独立进程，所以要自己选（§5.1）。

## 4. 高频原始数据获取（问题 3）

### 4.1 Bybit V5 实时 WebSocket（事实）

`orderbook.{depth}.{symbol}`（linear）[BB-ob]：

| 深度 | 推送间隔 | 消息类型 |
|---|---|---|
| 1 | 10 ms | **只推快照**；3 秒无变化时重推同一快照（`u` 不变） |
| 50 | 20 ms | 首条快照，之后增量 |
| 200 | 100 ms | 同上 |
| 1000 | 200 ms | 同上 |

- 字段：`ts`（系统生成时间 ms）、`cts`（撮合引擎时间，可与逐笔的 `T` 对齐）、`data.u`（update ID）、`data.seq`（跨深度的序列号，小者早）、`b`/`a`（`[price, size]`，买降序、卖升序）。增量规则：size=0 删除、新价位插入、已有价位改量。
- 恢复语义：收到新快照就重置本地簿；Bybit 出问题时会重发快照；服务重启时偶尔出现 `u=1` 的快照，要覆盖本地簿。**文档没有给出用 `u` 做丢包检测的步骤**（推断：多档增量可检查 `u` 是否连续，断档即重订阅取快照；本次实测见 §4.3）。
- RPI（Retail Price Improvement）挂单不出现在这些消息里，另有 RPI 盘口流。[BB-ob]
- REST `GET /v5/market/orderbook`：linear `limit` 1–1000（默认 25），`u` 与 1000 档 WS 一致，同样排除 RPI 挂单。[BB-ob-rest]

`publicTrade.{symbol}` [BB-trade]：

- 实时推送，`type=snapshot`；字段 `T`（成交时间 ms）、`s`、`S`（taker 方向）、`v`、`p`、`L`（tick direction）、`i`（成交 ID）、`BT`（大宗）、`RPI`、`seq`（跨序列号）。一条消息最多 1024 笔，多条消息可共享同一 `seq`。
- 断线补数：REST `GET /v5/market/recent-trade` linear 最多 1000 条、**不支持按时间区间查**；文档让历史去网站下载归档。[BB-recent] 所以断线超过“最近 1000 笔”的空窗，实时侧补不回来，只能等次日归档（推断）。

### 4.2 历史数据：官方下载

**Bybit `public.bybit.com`（实测 + 事实）**[BB-pub]：

- 根目录：`kline_for_metatrader4/`、`premium_index/`、`spot_index/`、`trading/`、`spot/`。**没有盘口目录。**
- `trading/BTCUSDT/`：`BTCUSDT2020-03-25.csv.gz` 起按日一个文件，共 2431 个；2026-11-19 的文件 `Last-Modified` 为 2026-11-20 01:12 UTC，其他几天也在次日 01:10 左右（即 T+1，约 1 小时）。
- 列：`timestamp,symbol,side,size,price,tickDirection,trdMatchID,grossValue,homeNotional,foreignNotional,RPI`。`timestamp` 是带 4 位小数的秒（100 µs 精度），`trdMatchID` 是 UUID。2026-11-19 当天 `RPI` 列全为 0（实测）。
- 没有 `seq`、没有 `cts`。和实时 `publicTrade` 用 `trdMatchID` ↔ `i` 对齐去重（推断，未逐条核对）。

**Bybit 盘口历史（作者宣称，未能读取页面）**：Bybit 官方 API 公告频道写明，“August 21, 2025 02:30 UTC — The historical futures OrderBook download data will be adjusted from level 500 @ 100 ms to level 200 @ 100 ms. Spot OrderBook download data remains unchanged.”，链接到 `https://www.bybit.com/derivatives/en/history-data`。[BB-ann] 该页（301 到 `/en/derivative-activity/history-data`）是浏览器渲染页面，本次两次抓取超时，**覆盖起始日、文件格式、更新延迟都没有查到一手说明**。同一公告还说 WS 盘口支持 1000 档（公告写 300 ms，现行文档写 200 ms）。[BB-ann]、[BB-ob]

**Binance `data.binance.vision`（实测 + 事实）**[BN-readme]、[BN-vision]：

- `futures/um/daily/` 下有 `aggTrades/ bookDepth/ bookTicker/ indexPriceKlines/ klines/ markPriceKlines/ metrics/ premiumIndexKlines/ trades/`。新的日文件“next day”可用；2026-11-19 的文件在 2026-11-20 07:01–07:37 UTC 上传（实测）。每个文件带 `.CHECKSUM`，本次 3 个文件 SHA-256 全部一致。
- `trades`（来自 `/fapi/v1/trades`）：`id,price,qty,quote_qty,time,is_buyer_maker`，毫秒时间戳；BTCUSDT 2026-11-19 共 **5,041,215 笔**，zip 38.1 MB（实测）。
- `aggTrades`：`agg_trade_id,price,quantity,first_trade_id,last_trade_id,transact_time,is_buyer_maker`；同日 1,990,886 条，zip 24.1 MB（实测）。
- `bookTicker`：BTCUSDT 只有 2023-05-16 到 **2024-03-30** 共 320 个日文件，之后没有（实测，与 [perp-and-calendar-data-sources.md](<./perp-and-calendar-data-sources.md>) 记录一致）。
- `bookDepth`：**不是盘口簿**，而是约每 30 秒一次、±0.2%/1%–5% 共 12 档价格带内的累计深度和名义金额（`timestamp,percentage,depth,notional`），同日 2,880 个时点、0.56 MB（实测）。只能估算冲击成本的量级，不能驱动 L2 撮合（推断）。
- 现货数据 2025-01-01 起时间戳改为微秒；归档文件可能事后被替换，README 列有替换清单。[BN-readme]

**Alpaca 美股（事实）**[ALP-md]、[ALP-rt]、[ALP-faq]、[ALP-hist]：

- 实时流 `wss://stream.data.alpaca.markets/v2/{iex|sip|delayed_sip}`，频道 `trades`、`quotes`、`bars`、`updatedBars`、`dailyBars`、`statuses`、`lulds` 等；订阅 `trades` 会自动收到 `corrections` 和 `cancelErrors`。逐笔字段 `i,x,p,s,c,t,z`，报价字段 `bx,bp,bs,ax,ap,as,c,t,z`；逐笔与报价时间戳为纳秒精度 RFC-3339。
- Basic（免费）：实时只有 IEX 一个交易所，WS 最多 30 个标的；历史自 2016 年，SIP 历史查询的 `end` 必须早于 15 分钟前；200 次/分钟。Algo Trader Plus（$99/月）：全美交易所、标的不限、10,000 次/分钟。`feed` 参数对所有股票端点通用，`iex` 是唯一不需订阅的 feed。
- 文档没有说明 IEX 报价是 IEX 自身的买一卖一还是 NBBO（推断：feed 名为 IEX，应视为 IEX 单所盘口，与全市场最优价不同，不能当美股的真实点差）。

### 4.3 实测：Bybit BTCUSDT 公共 WS 推送率（5 分钟）

2026-11-20 约 18:05–18:10 UTC，`wss://stream.bybit.com/v5/public/linear` 单连接同时订阅四个主题，310 秒。

| 主题 | 消息/秒 | 原始 JSON 字节/秒 | 每消息档位或笔数 | 消息类型 | `u` 断档 |
|---|---|---|---|---|---|
| `orderbook.1.BTCUSDT` | 45.7 | 8,832 | 2 档（买一 + 卖一） | 全部 snapshot（14,161 条） | 0 |
| `orderbook.50.BTCUSDT` | 43.0 | 17,894 | 13.3 个价位变化 | 1 快照 + 13,325 增量 | 0 |
| `orderbook.200.BTCUSDT` | 9.7 | 21,832 | 103.6 个价位变化 | 1 快照 + 3,016 增量 | 0 |
| `publicTrade.BTCUSDT` | 19.6 | 9,378 | 45.2 笔/秒 | 全部 snapshot | — |

- `orderbook.1` 每条消息的买一卖一都与上一条不同（45.7 次变化/秒 = 消息率），所以“只在变化时落盘”对 BTCUSDT 几乎不省条数。
- 这 5 分钟的成交速率（45 笔/秒）约为 2026-11-19 全天平均（29.5 笔/秒）的 1.5 倍，下面的日推算按“观测窗口 × 0.65 ～ × 1”给范围。
- 推断（按每秒速率 × 86,400，未落盘实测压缩率）：
  - L1：约 260 万–400 万条/日；原始 JSON 0.5–0.76 GB/日。每条存 `ts, cts, 本机接收时间, bid_px, bid_sz, ask_px, ask_sz, u, seq` 九列，按 zstd 后 6–12 B/条估算，约 **15–50 MB/日**。
  - 50 档增量：约 3,200 万–4,900 万个价位变化/日；原始 JSON 1.0–1.5 GB/日；按 3–6 B/价位变化估算 **100–300 MB/日**。
  - 200 档增量：约 5,600 万–8,700 万个价位变化/日；原始 JSON 1.2–1.9 GB/日；估算 **170–520 MB/日**。
  - 逐笔：实时速率与 §5.2 的归档实测同量级，用 §5.2 的数字。
- 这是单个 5 分钟窗口的数字，周末、行情剧烈时可能差数倍；要定存储预算，应落盘 1–2 周再测。

## 5. 存储量级与布局（问题 4）

### 5.1 Nautilus 数据目录布局（事实）[NT-cat]

- 目录按数据类型 + 标的：`catalog/data/quotes/<INSTRUMENT>/`、`catalog/data/trades/<INSTRUMENT>/`；文件名是内容的时间范围 `{start}_{end}.parquet`（ISO 8601，`:` `.` 换成 `-`），例如 `2024-01-01T00-00-00-000000000Z_2024-01-01T23-59-59-999999999Z.parquet`。旧的 `data/trade_tick/`、`data/quote_tick/` 布局不再被查询读取。
- 默认 `zstd` level 1、row group 最多 131,072 行、批 10,000 行。
- `consolidate_catalog_by_period(period_nanos=DAY_NS)` 可把文件切成按日（或按小时）一个；合并前检查区间无重叠无空洞。也就是说 Nautilus 的“分区”是“标的目录 + 文件名里的时间区间”，不是 Hive 风格 `date=` 目录。

### 5.2 实测：BTCUSDT 永续逐笔一天有多大

数据：Bybit `BTCUSDT2026-11-19.csv.gz`（2026-11-19 UTC 全天）。Parquet 用 pyarrow 26.0.1，与 Nautilus 默认相同的 zstd-1、row group 131,072。

| 项 | 数值 |
|---|---|
| 成交笔数 | 2,550,610 |
| 每分钟笔数：中位 / p99 / 最大 | 1,237 / 7,934 / 20,256 |
| 不同时间戳（100 µs 精度）| 1,032,940 |
| 官方 `.csv.gz` | 92.4 MB |
| 解压 CSV | 320.0 MB |
| Parquet，原样 11 列，zstd-1 | 87.2 MB（34.2 B/笔） |
| Parquet，精简 5 列（`ts_event` int64 ns、`price` f64、`size` f64、`aggressor_buy` bool、`trade_id` UUID 字符串），zstd-1 | 66.1 MB（25.9 B/笔） |
| 同上但不存 `trade_id`，zstd-1 / zstd-9 | 14.7 MB / 11.4 MB（5.7 / 4.5 B/笔） |

- 同一文件夹其他日子的 gz 体积（HEAD 读到）：2026-11-18 79.1 MB、2026-11-15 126.8 MB、2026-10-01 54.8 MB、2025-11-19 113.0 MB。单日波动约 2 倍（实测）。
- Binance 同日 BTCUSDT 永续逐笔 504 万笔（约为 Bybit 的 2 倍），zip 38.1 MB。
- 推断（按 2026-11-19 的比率外推，日成交量波动 ±2 倍）：Bybit BTCUSDT 一年约 9 亿笔；精简不带 ID 约 5–6 GB/年，带 UUID 约 24 GB/年，原样保留官方 gz 约 33 GB/年。UUID 可以存成 16 字节定长二进制，预计每笔约 +16 B（未测）。

## 6. 对 Talaria 的启示（推断，供选择，不替用户决定）

### 6.1 模拟成交放在哪

三种可选位置，前提都是“回测与模拟盘调用同一个成交模块”（Nautilus、LEAN 的做法；Hummingbot 两套代码是反例）：

- **A. 放在策略进程内（Nautilus Sandbox / LEAN PaperBrokerage 形态）**：策略进程订阅常驻服务的行情 WS，本进程内的 `SimExecutionAdapter` 撮合并写本进程的账本。回测时同一模块吃 Parquet 回放。
  - 好处：回测和模拟盘代码路径最一致，确定性（法则 ⑥）容易保证；常驻服务保持只读行情，不承载账本状态；与 D2“策略崩溃不拖垮服务”一致。
  - 代价：模拟盘的撮合输入是“经本机 WS 到达的行情”，与常驻服务看到的顺序可能有差（本机 WS 1–2 ms，D2 实测）；多个策略的模拟账户彼此独立，没有统一视图；WS 积压/断线时撮合会漏掉中间的 L1/逐笔，需要定义“漏数据时挂单怎么算”。
- **B. 放在常驻服务内（模拟交易所服务）**：策略进程只发订单意图，服务端用最原始的行情顺序撮合并持有模拟账本，前端可直接展示。
  - 好处：撮合拿到的是第一手行情顺序，不受策略进程积压影响；所有模拟账户一个视图；图表上的执行屏障投影（`EXECUTION_BARRIER`）数据就在服务里。
  - 代价：服务多了有状态的订单/账本写入（违背“行情服务只读”的简单性，需要按 ADR-0006 单独存储）；回测在策略进程内跑，模拟盘在服务里跑，要保证是同一个库、同一份配置，否则两套口径；订单往返多一跳 IPC（对 1m 策略可忽略）。
- **C. 独立的“模拟交易所”进程**：撮合 + 模拟账本单独一个进程，订阅服务行情，接策略订单。好处是隔离最好、以后换成实盘适配器的位置最清楚；代价是多一个进程的启停和监控，D1 否决过“按功能拆多进程”。

不论选哪个，都要先定一条：成交模块是一个纯库（输入行情事件 + 订单命令，输出成交事件），回测、模拟盘、反事实沙箱（`CounterfactualSimSandbox`）都调用它。位置只决定谁喂数据和谁持久化账本。

### 6.2 存哪些高频数据

| 方案 | 能支持的仿真 | 每标的存储（BTCUSDT 量级，推断） | 历史来源 |
|---|---|---|---|
| ① 只存 1m K 线（现状） | 法则 ①③④ 的规则化版本；拿不到点差 | 可忽略 | REST 可回溯到上线 |
| ② K 线 + L1 盘口（`orderbook.1` 变化时记录） | 吃单点差、挂单“穿过买一/卖一才成交”、秒级延迟墙 | 约 15–50 MB/日（5–18 GB/年，§4.3 推算） | **没有免费历史**：Bybit 归档无 L1；Binance `bookTicker` 2024-03-30 后停更。只能从开始落盘那天起积累 |
| ③ ② + 逐笔 | 挂单按对手方真实成交判定并受成交量上限、同根 K 线止损/止盈真实先后、成交量冲击滑点的近似 | 不带 ID 约 15 MB/日（5–6 GB/年），带 ID 约 66 MB/日 | Bybit `public.bybit.com` 逐笔可回溯到 2020-03，T+1；实时落盘与归档用成交 ID 对齐 |
| ④ ③ + 多档（50 或 200 档增量） | 冲击滑点按真实簿深度、排队位置（Nautilus 式需 L2 + 逐笔） | 50 档约 100–300 MB/日，200 档约 170–520 MB/日（§4.3 推算；约 40–190 GB/年） | Bybit 历史下载页（200 档@100ms，需浏览器、格式未核实）；否则只能自己落盘 |

取舍要点：

- 对 1m 级日内、以市价/止损单为主的策略，③ 的边际收益最大：历史可免费回补多年，而且直接解决法则 ③（同根 K 线先后）这一 1m K 线解决不了的问题。
- L1 的价值主要是点差和挂单判定；它没有免费历史，越早落盘越有用。若选 ②/③，建议记录 `ts`（交易所）、`cts`、本机接收时间、`u`、`seq`，并只在内容变化时落一条（`orderbook.1` 3 秒无变化会重推同一快照）。
- 多档的主要用途是滑点和排队，对 1m 策略的影响在小仓位下有限；存储与断档处理（`u` 连续性、重订阅快照）成本最高。可以只对少数主力合约开启，或推迟。
- 成交 ID 是体积大头：只为对齐去重的话，可以只在“实时落盘与归档重叠的那几天”保留，或改存 16 字节二进制。
- 美股：免费层的 IEX 报价不能代表全市场点差；要做美股 L1/逐笔仿真，前提是 SIP（$99/月）或接受“15 分钟前的 SIP 历史 + IEX 实时”混用并在数据里标注 feed（已有调研同样建议）。

### 6.3 历史从哪里补

- 逐笔：Bybit `public.bybit.com/trading/{SYMBOL}/` 每日 gz（免费、无需 Key、T+1 约 01:10 UTC）；Binance `data.binance.vision` 的 `trades`/`aggTrades` 作为对照或第二场所（T+1 约 07:00 UTC，带 SHA-256）。
- 实时断线：Bybit `recent-trade` 只能拿最近 1000 笔，断线较久时以次日归档覆盖当日落盘（推断：落盘文件按“实时版 / 归档校正版”区分，归档到达后替换）。
- L1/多档：Bybit 官方下载页（若用户愿意手动下载，需先在浏览器核实格式与条款）；或第三方付费数据（本文未调研，不属一手）；或从现在起自行落盘。

## 7. 没有查到一手资料的问题

- Bybit 历史数据下载页（`www.bybit.com/derivatives/en/history-data`）的覆盖范围、文件格式、更新延迟、是否含 L1：页面需浏览器渲染，两次抓取超时；只有 API 公告里的一句“200 档@100ms”。
- Bybit 文档没有给出用 `u`/`seq` 检测增量丢包的步骤。
- `public.bybit.com` 的逐笔归档与实时 `publicTrade` 是否逐条一致（含 RPI、大宗成交）没有官方说明；本次只看到当日 `RPI` 列全为 0。
- Hummingbot 没有对“只用 K 线回测”的官方局限说明。
- Alpaca 文档没说明 IEX feed 的 `quotes` 是否为 IEX 单所买一卖一；也没有免费层 WS 连接数上限。
- Nautilus 官方没有给出 `bar_adaptive_high_low_ordering` 的准确率数字，只引用了一份 EUR/USD 探索性分析。

## 来源

### NautilusTrader（`0db3667a3c7b13e01f12a9bf446616c571f1fce0`）

- [NT-bar] [docs/concepts/backtesting/bar-execution.md L1–153：K 线拆四点、`ts_init`、自适应高低顺序、下单时机、无下一根开盘成交](https://github.com/nautechsystems/nautilus_trader/blob/0db3667a3c7b13e01f12a9bf446616c571f1fce0/docs/concepts/backtesting/bar-execution.md#L1-L153)
- [NT-bar-src] [crates/execution/src/matching_engine/mod.rs L1968–1990、L2104–2112、L2406–2408：`process_bar` 与 `bar_high_first`](https://github.com/nautechsystems/nautilus_trader/blob/0db3667a3c7b13e01f12a9bf446616c571f1fce0/crates/execution/src/matching_engine/mod.rs#L2406-L2408)
- [NT-data] [docs/concepts/backtesting/data-and-venues.md L1–93：数据粒度、book_type 对照表、不能从低粒度生成 L2/L3](https://github.com/nautechsystems/nautilus_trader/blob/0db3667a3c7b13e01f12a9bf446616c571f1fce0/docs/concepts/backtesting/data-and-venues.md#L1-L93)
- [NT-fm] [docs/concepts/backtesting/fill-models.md L7–53、L163–192：按簿类型的行为、内置模型、概率参数](https://github.com/nautechsystems/nautilus_trader/blob/0db3667a3c7b13e01f12a9bf446616c571f1fce0/docs/concepts/backtesting/fill-models.md#L7-L53)
- [NT-fill] [docs/concepts/backtesting/fill-prices-and-matching.md L38–94、L145–213：L1/L2 成交价、K 线触发单跳空规则、Mark 触发、簿不可变与流动性消耗](https://github.com/nautechsystems/nautilus_trader/blob/0db3667a3c7b13e01f12a9bf446616c571f1fce0/docs/concepts/backtesting/fill-prices-and-matching.md#L38-L94)
- [NT-trade] [docs/concepts/backtesting/trade-execution.md L1–114：逐笔驱动撮合、主动方、簿与逐笔结合](https://github.com/nautechsystems/nautilus_trader/blob/0db3667a3c7b13e01f12a9bf446616c571f1fce0/docs/concepts/backtesting/trade-execution.md#L1-L114)
- [NT-queue] [trade-execution.md L116–200：队列位置、Sandbox 配置、队列生命周期](https://github.com/nautechsystems/nautilus_trader/blob/0db3667a3c7b13e01f12a9bf446616c571f1fce0/docs/concepts/backtesting/trade-execution.md#L116-L200)
- [NT-sbx] [crates/adapters/sandbox/README.md L9–28](https://github.com/nautechsystems/nautilus_trader/blob/0db3667a3c7b13e01f12a9bf446616c571f1fce0/crates/adapters/sandbox/README.md#L9-L28)；[crates/adapters/sandbox/src/execution.rs L76–82、L1086–1115：Sandbox 持有并新建 `OrderMatchingEngine`](https://github.com/nautechsystems/nautilus_trader/blob/0db3667a3c7b13e01f12a9bf446616c571f1fce0/crates/adapters/sandbox/src/execution.rs#L1086-L1115)
- [NT-sbx-lat] [docs/concepts/backtesting/execution-flow.md L100–160：inflight 释放规则、Sandbox 入向延迟](https://github.com/nautechsystems/nautilus_trader/blob/0db3667a3c7b13e01f12a9bf446616c571f1fce0/docs/concepts/backtesting/execution-flow.md#L100-L160)
- [NT-exch] [crates/backtest/src/exchange.rs L147、L527：`SimulatedExchange` 建 `OrderMatchingEngine`](https://github.com/nautechsystems/nautilus_trader/blob/0db3667a3c7b13e01f12a9bf446616c571f1fce0/crates/backtest/src/exchange.rs#L527)
- [NT-cat] [docs/concepts/data/catalog.md L79–93、L160–190、L536–570：压缩与 row group、文件命名与目录、按周期合并](https://github.com/nautechsystems/nautilus_trader/blob/0db3667a3c7b13e01f12a9bf446616c571f1fce0/docs/concepts/data/catalog.md#L160-L190)

### QuantConnect LEAN（`53236fa4a23433a96c7f57cbe7850dba9be840ac`）

- [L-dbm] [Common/Brokerages/DefaultBrokerageModel.cs L222–244（按证券类型选 FillModel）、L281–283（默认 NullSlippageModel）](https://github.com/QuantConnect/Lean/blob/53236fa4a23433a96c7f57cbe7850dba9be840ac/Common/Brokerages/DefaultBrokerageModel.cs#L222-L283)
- [L-fm] [Common/Orders/Fills/FillModel.cs L479–484：“There is no good way to model limit orders with OHLC”](https://github.com/QuantConnect/Lean/blob/53236fa4a23433a96c7f57cbe7850dba9be840ac/Common/Orders/Fills/FillModel.cs#L479-L484)
- [L-fm-market] [FillModel.cs L273–338：市价单、陈旧数据、滑点、整笔成交](https://github.com/QuantConnect/Lean/blob/53236fa4a23433a96c7f57cbe7850dba9be840ac/Common/Orders/Fills/FillModel.cs#L273-L338)
- [L-fm-stop] [FillModel.cs L347–398：止损市价单](https://github.com/QuantConnect/Lean/blob/53236fa4a23433a96c7f57cbe7850dba9be840ac/Common/Orders/Fills/FillModel.cs#L347-L398)
- [L-fm-limit] [FillModel.cs L705–760：限价单](https://github.com/QuantConnect/Lean/blob/53236fa4a23433a96c7f57cbe7850dba9be840ac/Common/Orders/Fills/FillModel.cs#L705-L760)
- [L-fm-prices] [FillModel.cs L1177–1232：`GetPrices` 取价顺序 Tick → QuoteBar → TradeBar](https://github.com/QuantConnect/Lean/blob/53236fa4a23433a96c7f57cbe7850dba9be840ac/Common/Orders/Fills/FillModel.cs#L1177-L1232)
- [L-efm] [Common/Orders/Fills/EquityFillModel.cs L182–240（止损与跳空）、L366–470（限价、严格穿价、DepthLimited TODO）](https://github.com/QuantConnect/Lean/blob/53236fa4a23433a96c7f57cbe7850dba9be840ac/Common/Orders/Fills/EquityFillModel.cs#L366-L470)
- [L-slip] [Common/Orders/Slippage/](https://github.com/QuantConnect/Lean/tree/53236fa4a23433a96c7f57cbe7850dba9be840ac/Common/Orders/Slippage)：`ConstantSlippageModel.cs`、`VolumeShareSlippageModel.cs` L44–80、`MarketImpactSlippageModel.cs` L30–40
- [L-paper] [Brokerages/Paper/PaperBrokerage.cs L29](https://github.com/QuantConnect/Lean/blob/53236fa4a23433a96c7f57cbe7850dba9be840ac/Brokerages/Paper/PaperBrokerage.cs#L29)；[Brokerages/Backtesting/BacktestingBrokerage.cs L239、L337：`Scan()` 调 `security.FillModel`](https://github.com/QuantConnect/Lean/blob/53236fa4a23433a96c7f57cbe7850dba9be840ac/Brokerages/Backtesting/BacktestingBrokerage.cs#L337)

### Hummingbot（`3351adad0a09f6a57a202219d3e52fe794672421`）

- [H-engine] [hummingbot/strategy_v2/backtesting/backtesting_engine_base.py L221–300：`run_backtesting` 默认 1m、`trade_cost`](https://github.com/hummingbot/hummingbot/blob/3351adad0a09f6a57a202219d3e52fe794672421/hummingbot/strategy_v2/backtesting/backtesting_engine_base.py#L221-L300)
- [H-pos] [hummingbot/strategy_v2/backtesting/executors_simulator/position_executor_simulator.py L10–103：入场按收盘、止损按高低、同时满足时取止盈](https://github.com/hummingbot/hummingbot/blob/3351adad0a09f6a57a202219d3e52fe794672421/hummingbot/strategy_v2/backtesting/executors_simulator/position_executor_simulator.py#L10-L103)
- [H-paper] [hummingbot/connector/exchange/paper_trade/paper_trade_exchange.pyx L121–143、L445–515、L825–870、L895：逐笔匹配挂单、`simulate_buy` 吃簿、盘口穿价触发挂单](https://github.com/hummingbot/hummingbot/blob/3351adad0a09f6a57a202219d3e52fe794672421/hummingbot/connector/exchange/paper_trade/paper_trade_exchange.pyx#L825-L870)

### Bybit

- [BB-ob] [V5 WebSocket Orderbook](https://bybit-exchange.github.io/docs/v5/websocket/public/orderbook)
- [BB-trade] [V5 WebSocket Trade](https://bybit-exchange.github.io/docs/v5/websocket/public/trade)
- [BB-ob-rest] [V5 Get Orderbook](https://bybit-exchange.github.io/docs/v5/market/orderbook)
- [BB-recent] [V5 Get Public Recent Trading History](https://bybit-exchange.github.io/docs/v5/market/recent-trade)
- [BB-pub] [public.bybit.com 归档站](https://public.bybit.com/)；[trading/BTCUSDT/](https://public.bybit.com/trading/BTCUSDT/)
- [BB-ann] [Bybit API Announcements（官方 Telegram 频道）：Upcoming Enhancements to OrderBook Data Services](https://t.me/s/Bybit_API_Announcements/354)；下载页 [history-data](https://www.bybit.com/derivatives/en/history-data)（未能读取）

### Binance

- [BN-readme] [binance-public-data README（`d571690eb66760763b0bd6e27e8cd35603c7e244`）](https://github.com/binance/binance-public-data/blob/d571690eb66760763b0bd6e27e8cd35603c7e244/README.md)
- [BN-vision] [data.binance.vision](https://data.binance.vision/)；目录清单取自其 S3 列表接口 `https://s3-ap-northeast-1.amazonaws.com/data.binance.vision?prefix=data/futures/um/daily/`

### Alpaca

- [ALP-md] [About Market Data API](https://docs.alpaca.markets/docs/about-market-data-api)
- [ALP-rt] [Real-time Stock Data](https://docs.alpaca.markets/docs/real-time-stock-pricing-data)
- [ALP-faq] [Market Data FAQ](https://docs.alpaca.markets/docs/market-data-faq)
- [ALP-hist] [Historical Stock Data](https://docs.alpaca.markets/docs/historical-stock-data-1)
