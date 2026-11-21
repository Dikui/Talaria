# 统一策略运行时：研究、回测、模拟盘、实时信号共用一套代码与取数接口

- 调研日期：2026-11-20。只使用官方文档、官方源码与规范；没有安装、运行外部项目，没有登录或调用收费 API。
- 范围：对应 [platform-redesign 需求](<../../.scratch/platform-redesign/spec.md>) 的空白 1：同一套策略代码如何跑 research / backtest / paper / live；策略与数据之间的接口；策略与数据引擎是否同进程，以及跨进程用什么；本机单用户、单写者存储下，Python 取数接口有哪些形态。
- 方法：浅克隆官方仓库，用固定提交读文档源码和实现；在线规范用 WebFetch 核对原文。没有测量任何延迟。
- 固定源码快照：
  - NautilusTrader `develop`：[`0db3667a3c7b13e01f12a9bf446616c571f1fce0`](https://github.com/nautechsystems/nautilus_trader/tree/0db3667a3c7b13e01f12a9bf446616c571f1fce0)（2026-11-20）。这是开发分支，不代表已发布稳定版。
  - QuantConnect LEAN `master`：[`53236fa4a23433a96c7f57cbe7850dba9be840ac`](https://github.com/QuantConnect/Lean/tree/53236fa4a23433a96c7f57cbe7850dba9be840ac)（2026-11-18）。
  - Hummingbot `master`：[`3351adad0a09f6a57a202219d3e52fe794672421`](https://github.com/hummingbot/hummingbot/tree/3351adad0a09f6a57a202219d3e52fe794672421)（2026-11-18）。选它作对照，是因为它用“可替换的数据提供者”做回测，和前两者的“整引擎换模式”是两种思路。vectorbt、backtrader 没有提供进程模型或研究/实盘同口径方面的新对照，本次不查。
- 标注约定：**事实**是源码或文档直接支持的内容；**作者宣称**是官方文档的定性说法，没有可复现的测量；**推断**是面向 Talaria 的建议，不是这些项目的功能。

不重复的已有结论：

- Nautilus 的执行面（RiskEngine 状态、exactly-once 限制、对账、单线程内核、一进程一节点）见 [agentic-trading-execution-sources.md §3](<./agentic-trading-execution-sources.md>)。本文只补数据侧和进程侧。
- ParquetDataCatalog 的并发、覆盖和 `as_of` 限制，以及 DuckDB 嵌入式的读写模式，见 [trading-data-storage-sources.md §3、§6](<./trading-data-storage-sources.md>)。
- 发布协议、多种时间戳和回放模式见 [trading-data-center-design.md §4–5](<./trading-data-center-design.md>)。

## 1. 结论先行

1. **两个必查项目都在同一进程内运行策略和数据引擎。**一种模式就是一套组件装配。Nautilus 的 backtest、sandbox、live 共用 `NautilusKernel`，从 `DataEngine` 往后的数据路径相同，只是数据入口不同（适配器经通道送入，或回测引擎直接喂入）。[NT3]、[NT8b] LEAN 用 `config.json` 的 `environments` 换掉 data-feed、history-provider、transaction、brokerage 等 handler；`live-paper` 用实时数据队列加 `PaperBrokerage`。[L1]、[L7] 两者都没有“策略进程通过 IPC 向独立数据服务取实时行情”的默认形态。
2. **跨进程在两者中都是可选的扩展，而且语义是 best-effort。**Nautilus 的外部 MessageBus 以 Redis Streams 为内置后端：先分发给进程内订阅者，再序列化交给后台任务写 Redis。有界的 egress 在队列满时**丢弃**消息，不对交易循环施加反压。[NT7b] 消费端节点用 `external_clients` 接收外部流，作为数据来源。[NT7c] 没有找到官方给出的进程内与 Redis 路径的延迟数字。
3. **研究环境要和策略同口径，关键是“同一个取数对象”，而不是同一个 Notebook。**LEAN 的 `QuantBook` 直接继承 `QCAlgorithm`，在 Notebook 进程里装配同一套 `HistoryProviderManager`、DataProvider、因子和映射文件；只把实时 data feed 换成 `NullDataFeed`，所以 `qb.History(...)` 和策略里的 `self.History(...)` 是同一套代码路径。[L2]、[L3] Nautilus 的研究取数是 `ParquetDataCatalog.query_*`，回测通过 `BacktestDataConfig` 读同一个目录，实时节点可以用 `LiveNodeConfig.catalogs` 让历史请求先查目录、再补缺口。[NT9]
4. **Hummingbot 是更轻的第三种做法：策略只依赖一个可注入的 `MarketDataProvider`，回测时换成子类 `BacktestingDataProvider`。**[H1]、[H2] 但它的回测先对整段历史一次性算出 `processed_data`，再模拟执行。[H3] 这要求策略里的特征计算本身是因果的，框架不替你挡住前视偏差（推断）。
5. **对 Talaria（推断）：要决定的是两件事。**一是策略跑在数据服务进程内还是独立进程；二是历史取数走 SDK 直读已发布的 Parquet 还是走服务。三个方案见 §6。本机多进程只读 Parquet 本身没有并发冲突，前提是文件发布后不可变（Parquet 的 footer 在文件末尾写入；DuckDB 文档只讨论数据库文件锁，没有给外部 Parquet 文件规定并发语义）。[S1]、[S2]

## 2. NautilusTrader：一个内核，三种环境

### 2.1 同一套策略代码怎么跑各个模式（事实）

| 模式 | 官方名称 | 数据来源 | 成交 |
|---|---|---|---|
| 研究 | 没有单独的运行时；Notebook 里直接用 `ParquetDataCatalog` 查询，或在 Notebook 中跑低层 `BacktestEngine` | 目录查询 | — |
| 回测 | `Backtest`（`BacktestEngine` 低层 / `BacktestNode` 高层） | 历史数据，由引擎直接喂入 | 模拟 |
| 模拟盘 | `Sandbox` | 实时数据 | 模拟 |
| 实盘 | `Live`（可以接 paper 或真实账户） | 实时数据 | venue |

来源：[NT3] 环境定义；[NT11] 两层回测 API 的选择条件；[NT12] 官方提供 Docker 化的 JupyterLab，教程脚本可以用 Jupytext 当 Notebook 打开。

- **作者宣称：**“The same strategy and execution-algorithm code can run across backtest and live systems”，但实盘仍会引入 venue、传输、时序、持久化和对账等差异。[NT1]、[NT10]
- **事实：**从 `DataEngine` 往后，三种环境走同一条路径：先写入 `Cache`，再按主题在 `MessageBus` 上发布，最后调用策略的 `on_quote/on_bar`。不同的只是入口：live/sandbox 由适配器经通道送入，回测由引擎直接喂入。[NT8b]、[NT4]

### 2.2 策略与数据之间的接口（事实）

- **订阅与请求分开：**`subscribe_bars()` 的数据进 `on_bar()`；`request_bars()` 返回关联 ID，历史批量进 `on_historical_bars()`。典型做法是先注册指标，再 request 历史数据预热，最后 subscribe 实时数据。注册顺序错了，指标就收不到历史更新。[NT8]
- **Cache：**同步读，写入成功后可以用 `self.cache.quote(instrument_id)` 读到。但回调里可能看到比当前事件更新的缓存状态，投递顺序不提供事件时间快照。[NT4]
- **MessageBus 主题：**实时数据在 `data.<kind>...`；请求、回放、工作流生成的数据在 `data.pipeline.<kind>...`，后者不承诺和实时相同的顺序与时序语义。[NT7a]
- **历史请求和目录：**开启 `streaming` feature 后，`LiveNodeConfig.catalogs` 让历史请求先读目录中已有的区间，缺的部分再找数据客户端要；`update_catalog=True` 时，只有能明确报告“请求完整结束”的客户端才回写目录。`LiveNodeConfig.catalogs` 不像 `BacktestDataConfig` 那样预加载数据。[NT9]
- **实时数据落盘：**运行时可以先写本地 Feather 暂存文件，再“promote”进目录。**promote 成功后**，目录查询才能看到这些数据；暂存 flush 和目录提交是两步。[NT9c]

### 2.3 进程模型（事实与作者宣称）

- **同进程、单线程内核：**MessageBus、策略回调、风控、Cache 读写都在同一个线程；网络、日志、持久化在 Tokio 后台任务里，通过通道交给内核线程。[NT5] Python 的 `LiveNode` 在调用线程上跑内核事件循环。[NT2]
- **一进程一节点：**同一进程不支持并发运行多个 `LiveNode`/`BacktestNode`；要并行或隔离负载，就每个节点一个进程。[NT6]
- **跨进程只有外部 MessageBus：**内置后端是 Redis（≥6.2），编码可以是 JSON、MessagePack，行情还可以用 SBE。消息先交给进程内订阅者，再序列化，经 MPSC 通道交给独立任务写 Redis Streams。出站调用不得阻塞总线线程，有界实现**在队列满时丢弃**。消费端节点通过 `external_clients` 把外部流当作数据源，并要求先发 book snapshot、再发增量。[NT7b]、[NT7c]
- **作者宣称：**外部流用于实时传输，durable 恢复属于 cache/event store。[NT3a]
- **内部队列：**live runner 的通道没有反压、合并或丢弃行情，也没有最大深度；持续输入超过派发能力时，队列深度、延迟和内存都会增长。[NT10b]
- **没查到：**官方文档没有给出进程内派发与 Redis 外部流之间的延迟测量。`networking.md` 有 HTTP 传输基准，但与本问题无关。

## 3. QuantConnect LEAN：handler 装配 + QuantBook

### 3.1 模式切换就是换 handler（事实）

`Launcher/config.json` 的 `environments`：[L1]

- `backtesting`：`FileSystemDataFeed` + `SubscriptionDataReaderHistoryProvider` + `BacktestingTransactionHandler`。
- `live-paper`：`LiveTradingDataFeed` + 数据队列 `LiveDataQueue` + `PaperBrokerage` + `BacktestingTransactionHandler`。注释写明“the paper brokerage requires the BacktestingTransactionHandler”，`PaperBrokerage` 本身继承 `BacktestingBrokerage`。[L7]
- 各家实盘 broker 环境：`history-provider` 是 `[BrokerageHistoryProvider, SubscriptionDataReaderHistoryProvider]` 的列表，`data-queue-handler` 由 broker 插件提供。

策略只面向 `QCAlgorithm` 的 API。实时数据进来的接口是 `IDataQueueHandler.Subscribe(SubscriptionDataConfig, EventHandler) -> IEnumerator<BaseData>`[L4]，`LiveTradingDataFeed` 继承 `FileSystemDataFeed`，再加上数据队列。[L5] 历史数据统一走 `IHistoryProvider.GetHistory(IEnumerable<HistoryRequest>, DateTimeZone) -> IEnumerable<Slice>`。[L4]

### 3.2 进程模型（事实）

- `Engine.Run` 在**同一进程**内创建算法实例、数据管理器和同步器（实盘用 `LiveSynchronizer`，回测用 `Synchronizer`）。算法主循环由 `Isolator` 在时间限制下执行 `AlgorithmManager.Run`，逐个 `TimeSlice` 调用 `OnData`。[L6]
- Python 策略也在同一进程中，通过 Python.NET 调用（`QuantBook` 构造函数中有 `Py.GIL()`、`Py.Import("pandas")`）。[L2]
- 本地 CLI 的研究环境 `lean research` 启动 JupyterLab（默认 `localhost:8888`），镜像是 `quantconnect/research:<tag>`；默认数据提供者是本机数据。[L8] 页面没有写数据目录怎么挂载。
- **没查到：**LEAN 官方的进程间行情传输方案或延迟数字。broker 和数据插件都是进程内组件。

### 3.3 研究环境如何同口径（事实）

`QuantBook : QCAlgorithm`。[L2] 构造函数做了这些事：

1. 用 `LeanEngineAlgorithmHandlers.FromConfiguration(composer, researchMode: true)` 创建与引擎同源的 handler（L129）。
2. 用 `NullDataFeed` 构造 `DataManager`，因此**没有事件驱动的实时数据流**（L187–195）。
3. 新建 `HistoryProviderManager`，并用同一 DataProvider、`ZipDataCacheProvider`、MapFile/FactorFile 初始化（L198–213）。
4. 起始时间设为纽约当前时间；早于 `qb-data-hour`（默认 9 点）时退一天（L103–113）。
5. 设置 `AlgorithmMode.Research`（L223）。

因此 Notebook 中的 `qb.History(...)` 就是 `QCAlgorithm.History(...)`（C# 重载见 History.cs L243–337；Python 返回 DataFrame 的重载见 Python.cs L1040–1114）。[L3] 官方文档的建议流程是“在 Research 里验证假设，再回测”，并用 Object Store 在两边传递模型。[L9]

**推断：**LEAN 实现同口径的方式不是共享一个进程，而是共享一套取数代码和同一份本地数据文件；QuantBook 每次自己在 Notebook 进程里装配一遍。

## 4. Hummingbot（对照）：注入式数据提供者

- **事实：**V2 controller 的构造参数是 `market_data_provider`。回测引擎 `run_backtesting` 把它换成 `BacktestingDataProvider(MarketDataProvider)`，并传入 `actions_queue=None`；随后调用一次 `controller.update_processed_data()`，再 `simulate_execution`。[H2]、[H3]
- **事实：**`MarketDataProvider` 的取数接口是同步方法，如 `get_candles_df(connector, pair, interval, max_records)`、`get_price_by_type`、`get_order_book`、`get_funding_info`；历史数据用 `async get_historical_candles_df(...)`。[H1]
- **事实：**跨进程只用于控制和事件转发：MQTT 的命令主题（start/stop/config/status/history……）和 `MQTTMarketEventForwarder`。行情仍然在 bot 进程内取。[H4]
- **推断：**“策略只认一个 Provider 接口，按模式注入不同实现”最接近“一两行代码取数”。但 Hummingbot 回测先对整段数据一次性算特征，这依赖策略作者自己保证因果性；Nautilus 和 LEAN 是逐事件推进，框架层面限制了策略能看到的数据范围。

## 5. 本机取数接口形态（事实 + 取舍推断）

| 形态 | 一手事实 | 对 Talaria 的取舍（推断） |
|---|---|---|
| 进程内 SDK 直读已发布 Parquet（pyarrow / DuckDB，按 manifest 列出的文件读） | Parquet 的元数据写在数据之后，读者先读 footer。[S2] DuckDB 的进程内并发文档只规定**数据库文件**：一个进程读写，或多个进程 `READ_ONLY`，通过文件锁实现；多进程写原生库要用 Quack 协议或 DuckLake+PostgreSQL。文档**没有**规定外部 Parquet 文件被并发修改时的语义。[S1] | 写者只做“写新文件→原子发布 manifest”，读者按 manifest 读不可变文件，多进程读就没有冲突，也不需要启动服务，最符合“一两行代码”。缺点是只能看到已发布的冷数据；按 ADR-0011，冷库的延迟取决于快照构建频率。 |
| SQLite WAL 只读连接 | 读者和写者互不阻塞；只能有一个写者；所有进程必须在同一主机，不能放在网络文件系统上。[S5] | Talaria 已在 ADR-0012 中采用。适合元数据、manifest 和小表，不适合大批量行情扫描。 |
| 本地 HTTP / WebSocket（数据服务暴露） | 无外部规范约束；Nautilus、LEAN 都没有把它作为策略取数路径。 | 能看到热端最新数据，前端可以共用；但多一跳序列化，需要定义反压或丢弃策略，否则会重现 Nautilus 文档里那种无界队列问题。[NT10b] |
| Arrow Flight | “an RPC framework for high-performance data services based on Arrow data”；方法包括 `GetFlightInfo/DoGet/DoPut/DoExchange` 等，基于 gRPC，并尽量避免 Protobuf 带来的额外拷贝。[S3] | 适合批量拉取 Arrow 表、跨语言调用；对单用户本机来说，多了 gRPC 依赖和服务端实现，相比直读 Parquet 的收益要靠实测证明，本次没有测。 |
| 共享内存 / 内存映射 Arrow IPC | Arrow IPC **文件**格式支持随机访问，与 `pa.memory_map` 搭配时读取零拷贝、不分配新内存；流格式只能顺序读。[S4] Python `SharedMemory` 可被多个进程访问，但需要自己管理 `close/unlink` 和 resource tracker，文档不提供同步机制。[S6] | 延迟潜力最高，但环形缓冲、序号和读写同步都要自研。在没有测出 IPC 是瓶颈之前，属于过度设计。 |
| 外部消息流（Redis Streams 等） | Nautilus 的内置做法；egress 在队列满时丢弃。[NT7b] | 引入 Redis 守护进程，违背“不用先启动一堆东西”。只有在需要多消费者回放或持久消费位点时才值得。 |

## 6. 对 Talaria 的启示（推断，供选择，不替用户决定）

共同前提（三个方案都适用）：

- 策略 API 只认一个取数对象（例如 `ctx.history(...)` 和 `on_bar`），回测、模拟盘、实时信号分别注入不同实现。这是 Nautilus、LEAN、Hummingbot 的共同点。
- 历史数据统一按 manifest 读取不可变 Parquet，Notebook 与回测用同一个 reader。这对应 QuantBook 的做法：同一份文件，同一段取数代码。
- 实时侧沿用 Nautilus 的“先预热历史，再订阅实时，指标先注册”的顺序约束。

### 方案 A：策略在数据服务进程内运行（Nautilus / LEAN 形态）

- 做法：常驻数据服务同时是策略宿主。实时 bar 由 feed 状态机直接回调策略，没有 IPC；Notebook 和脚本用 SDK 直读已发布的 Parquet，热端最新数据经本地 HTTP 取。
- 优点：实时延迟最低，没有额外的序列化和反压设计；与 Nautilus 单线程内核同构，回测和实时的事件顺序最容易一致。
- 代价：策略代码（包括 agent 生成的代码）的异常、阻塞或内存问题直接影响数据服务和图表；改策略要热加载或重启服务；需要像 Nautilus 那样把策略回调和 I/O 隔离到不同线程或协程，ADR-0012 的写线程模型要随之扩展。

### 方案 B：数据服务是单写者，策略跑在独立进程（Nautilus 外部流消费者形态）

- 做法：数据服务只负责采集和发布；每个策略或模拟盘一个进程，通过本机 WebSocket 或 Unix socket 订阅实时 bar；历史数据由 SDK 直读 Parquet。
- 优点：故障隔离，可以随时单独启停，符合“各功能单独启动”；单写者约束最清楚。
- 代价：多一跳，延迟要实测（官方没有可借用的数字）；必须定义队列满时丢弃还是反压，以及断线重连后的补齐（用 history 补缺口，类似 `LiveNodeConfig.catalogs` 的“先读目录、再补缺”）；实时路径和回测路径的事件顺序可能不同。

### 方案 C：默认独立进程，保留进程内“快车道”

- 做法：取数和策略 API 与方案 B 相同；对延迟敏感、代码可信的策略，允许以插件形式加载到数据服务进程内运行，API 不变，只换注入的实现。
- 优点：日常研究和 agent 策略默认隔离，少数策略可以拿到方案 A 的延迟；迁移只改配置。
- 代价：要同时维护两种宿主，测试矩阵翻倍；“可信”的判定和资源限制要单独设计。只有在实测证明方案 B 的延迟不够时才值得做。

无论选哪个方案，先用现有 feed 和回放做一次本机测量（数据服务收到 bar → 策略回调的 p50/p99，分别测进程内和本机 WS），再决定。目前没有一手数字支持任一方案在延迟上“够用”或“不够用”。

## 7. 没有查到一手资料的问题

- NautilusTrader 和 LEAN 都没有官方的进程内与跨进程（Redis 等）行情延迟测量；Nautilus 只有 HTTP 传输基准，与策略取数无关。
- DuckDB 和 Arrow 都没有规定“外部 Parquet 文件在被读时被替换或追加”的语义；本文的“不可变文件 + manifest”结论是推断，依据是 Parquet 的 footer 写入方式和已有的发布协议调研。
- `lean research` 文档没有说明 Docker 中数据目录的挂载方式。
- Arrow Flight 在本机回环上的延迟，以及与直读 Parquet 的对比，没有官方数字。

## 来源

### NautilusTrader（`0db3667a3c7b13e01f12a9bf446616c571f1fce0`）

- [NT1] [overview.md L1–18：同一策略代码跨回测与实盘，以及差异](https://github.com/nautechsystems/nautilus_trader/blob/0db3667a3c7b13e01f12a9bf446616c571f1fce0/docs/concepts/overview.md#L1-L18)
- [NT2] [overview.md L90–111：common core、回测、LiveNode 在调用线程跑内核](https://github.com/nautechsystems/nautilus_trader/blob/0db3667a3c7b13e01f12a9bf446616c571f1fce0/docs/concepts/overview.md#L90-L111)
- [NT3] [architecture.md L148–164：Backtest/Sandbox/Live 与共享 NautilusKernel](https://github.com/nautechsystems/nautilus_trader/blob/0db3667a3c7b13e01f12a9bf446616c571f1fce0/docs/concepts/architecture.md#L148-L164)；[NT3a] [L80–89：外部 backing 是实时传输，恢复属于 cache/event store](https://github.com/nautechsystems/nautilus_trader/blob/0db3667a3c7b13e01f12a9bf446616c571f1fce0/docs/concepts/architecture.md#L80-L89)
- [NT4] [architecture.md L166–221：一个 QuoteTick 从适配器到策略的路径及 Cache 可见性](https://github.com/nautechsystems/nautilus_trader/blob/0db3667a3c7b13e01f12a9bf446616c571f1fce0/docs/concepts/architecture.md#L166-L221)
- [NT5] [architecture.md L470–500：单线程内核与后台线程](https://github.com/nautechsystems/nautilus_trader/blob/0db3667a3c7b13e01f12a9bf446616c571f1fce0/docs/concepts/architecture.md#L470-L500)
- [NT6] [architecture.md L709–728：一进程一节点](https://github.com/nautechsystems/nautilus_trader/blob/0db3667a3c7b13e01f12a9bf446616c571f1fce0/docs/concepts/architecture.md#L709-L728)
- [NT7a] [message_bus.md L19–33：data 与 data.pipeline 主题](https://github.com/nautechsystems/nautilus_trader/blob/0db3667a3c7b13e01f12a9bf446616c571f1fce0/docs/concepts/message_bus.md#L19-L33)；[NT7b] [L328–443：外部 egress/ingress、满队列丢弃、Redis MPSC 任务](https://github.com/nautechsystems/nautilus_trader/blob/0db3667a3c7b13e01f12a9bf446616c571f1fce0/docs/concepts/message_bus.md#L328-L443)；[NT7c] [L669–697：外部流生产者与消费者节点](https://github.com/nautechsystems/nautilus_trader/blob/0db3667a3c7b13e01f12a9bf446616c571f1fce0/docs/concepts/message_bus.md#L669-L697)
- [NT8] [data/index.md L313–372：request 与 subscribe、指标注册顺序](https://github.com/nautechsystems/nautilus_trader/blob/0db3667a3c7b13e01f12a9bf446616c571f1fce0/docs/concepts/data/index.md#L313-L372)；[NT8b] [L513–527：DataEngine 往后三种环境路径相同](https://github.com/nautechsystems/nautilus_trader/blob/0db3667a3c7b13e01f12a9bf446616c571f1fce0/docs/concepts/data/index.md#L513-L527)
- [NT9] [data/catalog.md L196–242：typed query、LiveNodeConfig.catalogs、回写规则](https://github.com/nautechsystems/nautilus_trader/blob/0db3667a3c7b13e01f12a9bf446616c571f1fce0/docs/concepts/data/catalog.md#L196-L242)；[L400–405：三种目录接入方式](https://github.com/nautechsystems/nautilus_trader/blob/0db3667a3c7b13e01f12a9bf446616c571f1fce0/docs/concepts/data/catalog.md#L400-L405)；[NT9c] [L620–637：Feather 暂存与 promote 后可见](https://github.com/nautechsystems/nautilus_trader/blob/0db3667a3c7b13e01f12a9bf446616c571f1fce0/docs/concepts/data/catalog.md#L620-L637)
- [NT10] [live.md L13–31：回测与实盘差异](https://github.com/nautechsystems/nautilus_trader/blob/0db3667a3c7b13e01f12a9bf446616c571f1fce0/docs/concepts/live.md#L13-L31)；[NT10b] [L351–360：runner 通道无反压、无界](https://github.com/nautechsystems/nautilus_trader/blob/0db3667a3c7b13e01f12a9bf446616c571f1fce0/docs/concepts/live.md#L351-L360)
- [NT11] [backtesting/apis-and-runs.md L1–21：低层/高层回测 API 的选择](https://github.com/nautechsystems/nautilus_trader/blob/0db3667a3c7b13e01f12a9bf446616c571f1fce0/docs/concepts/backtesting/apis-and-runs.md#L1-L21)
- [NT12] [getting_started/index.md L82–97：Docker JupyterLab](https://github.com/nautechsystems/nautilus_trader/blob/0db3667a3c7b13e01f12a9bf446616c571f1fce0/docs/getting_started/index.md#L82-L97)；[tutorials/index.md L8：Jupytext](https://github.com/nautechsystems/nautilus_trader/blob/0db3667a3c7b13e01f12a9bf446616c571f1fce0/docs/tutorials/index.md#L8)

### QuantConnect LEAN（`53236fa4a23433a96c7f57cbe7850dba9be840ac`）

- [L1] [Launcher/config.json L376–403：backtesting 与 live-paper 环境的 handler 装配](https://github.com/QuantConnect/Lean/blob/53236fa4a23433a96c7f57cbe7850dba9be840ac/Launcher/config.json#L376-L403)
- [L2] [Research/QuantBook.cs L49–229：QuantBook 继承 QCAlgorithm，NullDataFeed 与 HistoryProviderManager](https://github.com/QuantConnect/Lean/blob/53236fa4a23433a96c7f57cbe7850dba9be840ac/Research/QuantBook.cs#L49-L229)
- [L3] [Algorithm/QCAlgorithm.History.cs L46、L243–337](https://github.com/QuantConnect/Lean/blob/53236fa4a23433a96c7f57cbe7850dba9be840ac/Algorithm/QCAlgorithm.History.cs#L243-L337)；[QCAlgorithm.Python.cs L1040–1114：Python History 重载](https://github.com/QuantConnect/Lean/blob/53236fa4a23433a96c7f57cbe7850dba9be840ac/Algorithm/QCAlgorithm.Python.cs#L1040-L1114)
- [L4] [Common/Interfaces/IDataQueueHandler.cs L29–37](https://github.com/QuantConnect/Lean/blob/53236fa4a23433a96c7f57cbe7850dba9be840ac/Common/Interfaces/IDataQueueHandler.cs#L29-L37)；[IHistoryProvider.cs L28–47](https://github.com/QuantConnect/Lean/blob/53236fa4a23433a96c7f57cbe7850dba9be840ac/Common/Interfaces/IHistoryProvider.cs#L28-L47)
- [L5] [Engine/DataFeeds/LiveTradingDataFeed.cs L41–102](https://github.com/QuantConnect/Lean/blob/53236fa4a23433a96c7f57cbe7850dba9be840ac/Engine/DataFeeds/LiveTradingDataFeed.cs#L41-L102)
- [L6] [Engine/Engine.cs L116、L352–363：同进程装配与 Isolator](https://github.com/QuantConnect/Lean/blob/53236fa4a23433a96c7f57cbe7850dba9be840ac/Engine/Engine.cs#L352-L363)；[AlgorithmManager.cs L191、L585：TimeSlice 循环与 OnData](https://github.com/QuantConnect/Lean/blob/53236fa4a23433a96c7f57cbe7850dba9be840ac/Engine/AlgorithmManager.cs#L191)
- [L7] [Brokerages/Paper/PaperBrokerage.cs L29](https://github.com/QuantConnect/Lean/blob/53236fa4a23433a96c7f57cbe7850dba9be840ac/Brokerages/Paper/PaperBrokerage.cs#L29)
- [L8] [LEAN CLI Research 文档](https://www.quantconnect.com/docs/v2/lean-cli/research)
- [L9] [Research Environment Getting Started](https://www.quantconnect.com/docs/v2/research-environment/key-concepts/getting-started)

### Hummingbot（`3351adad0a09f6a57a202219d3e52fe794672421`）

- [H1] [data_feed/market_data_provider.py L28–72、L523–540](https://github.com/hummingbot/hummingbot/blob/3351adad0a09f6a57a202219d3e52fe794672421/hummingbot/data_feed/market_data_provider.py#L28-L72)
- [H2] [strategy_v2/backtesting/backtesting_data_provider.py L21–132](https://github.com/hummingbot/hummingbot/blob/3351adad0a09f6a57a202219d3e52fe794672421/hummingbot/strategy_v2/backtesting/backtesting_data_provider.py#L21-L132)
- [H3] [strategy_v2/backtesting/backtesting_engine_base.py L221–256](https://github.com/hummingbot/hummingbot/blob/3351adad0a09f6a57a202219d3e52fe794672421/hummingbot/strategy_v2/backtesting/backtesting_engine_base.py#L221-L256)
- [H4] [remote_iface/mqtt.py L81–128、L350–390](https://github.com/hummingbot/hummingbot/blob/3351adad0a09f6a57a202219d3e52fe794672421/hummingbot/remote_iface/mqtt.py#L81-L128)

### 规范与库文档

- [S1] [DuckDB 2.0 Concurrency](https://duckdb.org/docs/2.0/connect/concurrency.html)
- [S2] [Apache Parquet File Format](https://parquet.apache.org/docs/file-format/)
- [S3] [Arrow Flight RPC 规范](https://arrow.apache.org/docs/format/Flight.html)
- [S4] [PyArrow Streaming, Serialization, and IPC](https://arrow.apache.org/docs/python/ipc.html)
- [S5] [SQLite Write-Ahead Logging](https://www.sqlite.org/wal.html)
- [S6] [Python multiprocessing.shared_memory](https://docs.python.org/3/library/multiprocessing.shared_memory.html)
