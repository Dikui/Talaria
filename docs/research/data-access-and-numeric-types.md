# 历史取数路径、SDK 返回形态与数值存储表示

- 调研日期：2026-11-20。
- 范围：对应 [platform-redesign 需求](<../../.scratch/platform-redesign/spec.md>) 待定决策 3（统一取数接口）中的三个子项：
  1. 历史读取路径：研究环境和策略直读文件，还是经服务读；“已落盘历史 + 最新未落盘部分”怎么拼接。
  2. 数值类型：存储里价格和数量用什么表示，怎么和 [ADR-0010](<../adr/0010-pure-python-indicator-core.md>) 的纯 Python `Decimal` 算子衔接。
  3. SDK 返回形态：同一套接口怎么同时给策略精确对象、给 Notebook 表格。
- 已有前提：D1（常驻服务是历史数据唯一写入方，自写 Parquet generation + SQLite 清单，见 [`cold_store.py`](<../../core/src/talaria/perception/cold_store.py>)）；D2（策略、回测、Notebook 在独立进程）。现状的冷库 schema 把 `open/high/low/close/volume/turnover` 都存成 `pa.string()`（`cold_store.py` L119–134）；其中的 `precision` 列是 `PrecisionTier` 枚举，不是小数位数。
- 方法：
  - 浅克隆官方仓库，按固定提交读文档源码和实现。NautilusTrader、LEAN 沿用 [unified-strategy-runtime-sources.md](<./unified-strategy-runtime-sources.md>) 的同一提交，便于交叉引用。
  - 用 WebFetch 核对 Apache Arrow、Parquet、pandas、Polars、DuckDB、Python、.NET 的在线官方文档原文。
  - 在系统临时目录 `/tmp/talaria-numeric-bench.yYL1U5/`（`mktemp -d` 新建）用一次性环境做基准和行为核对，没有往项目 `.venv` 安装任何包，也没有改仓库代码。命令：`uv run --no-project --python .venv/bin/python --with pyarrow --with polars --with pandas --with duckdb --with numpy python <脚本>`。脚本、日志和生成的 Parquet 都留在该目录（附录 A）。
- 固定源码快照：
  - NautilusTrader `develop`：[`0db3667a`](https://github.com/nautechsystems/nautilus_trader/tree/0db3667a3c7b13e01f12a9bf446616c571f1fce0)（2026-11-20）。这是 v2 开发分支（Rust 核心 + PyO3 Python 包，`RELEASES.md` 顶部为 “2.0.0 Released on TBD”），不代表已发布稳定版。
  - QuantConnect LEAN `master`：[`53236fa4`](https://github.com/QuantConnect/Lean/tree/53236fa4a23433a96c7f57cbe7850dba9be840ac)（2026-11-18）。
  - ArcticDB `master`：[`0c1db332`](https://github.com/man-group/ArcticDB/tree/0c1db33297e16007d501471173e5d36f1ed1845f)（2026-11-20）。
  - OpenBB `develop`：[`f0f1852b`](https://github.com/openbq-org/OpenBB/tree/f0f1852b96dd3fc4d5868f8863cbe053ff49c6cd)（2026-11-18；官方仓库已从 `OpenBB-finance/OpenBB` 重定向到 `openbq-org/OpenBB`，见 [trading-data-templates-sources.md](<./trading-data-templates-sources.md>)）。
- 在线文档版本：Apache Arrow / PyArrow v26.0.1；pandas 3.1.1；Polars Python 2.1.0；DuckDB 2.0；Python 3.15.2。
- 标注约定：
  - **事实**：官方文档、规范或源码直接写明的内容。
  - **作者宣称**：官方的定性说法，没有独立复现。
  - **实测**：附录实验的观察，只代表本机、本版本、单次短时运行。
  - **推断**：面向 Talaria 的判断，不是这些项目的功能。

不重复的已有结论（只链接）：

- Nautilus / LEAN 的进程模型、QuantBook 与策略同口径取数、`LiveNodeConfig.catalogs` 的基本用法：[unified-strategy-runtime-sources.md](<./unified-strategy-runtime-sources.md>) §1–§3。
- ParquetDataCatalog 的并发、覆盖与 `as_of` 限制，DuckDB 嵌入式读写模式：[trading-data-storage-sources.md](<./trading-data-storage-sources.md>) §3、§6。
- DuckLake 能否替代自写清单：[ducklake-evaluation.md](<./ducklake-evaluation.md>)。
- OpenBB 的许可证、provider 模板：[trading-data-templates-sources.md](<./trading-data-templates-sources.md>) §3.1。

## 1. 结论先行

1. **四个先例里，历史数据都由调用方进程直接读存储，没有一个必须经数据服务转发。**Nautilus 由 Python 进程内的 Rust/DataFusion 直接扫 Parquet 目录；LEAN 由引擎进程内的 `SubscriptionDataReaderHistoryProvider` 读本地 zip/CSV；ArcticDB 文档明确说核心功能不需要服务器；OpenBB 的 `obb` 在进程内直接调用 provider，`openbb-api` 只是把同一批命令另包成 FastAPI。（事实，[NT-C1]、[L-H1]、[A-1]、[O-3]）
2. **“落盘历史 + 最新部分”的拼接，两个交易框架都在引擎内按时间戳完成，并都显式处理重叠。**Nautilus 实盘节点对历史请求先算目录覆盖区间，目录给已覆盖部分，缺口区间再向交易所数据客户端请求，多段响应汇总回同一个请求 ID；订阅时还会把目录里的最后时间戳作为 `start_ns` 预填给客户端。（事实，[NT-E1]、[NT-E2]）LEAN 实盘预热时把“本地文件枚举器”和“历史接口枚举器”串接并丢弃重复时间戳，再接到实时枚举器后面；文件枚举器的最后一个点会作为历史枚举的起点。（事实，[L-W1]、[L-W2]）
3. **数值类型上，Nautilus v2 已经从私有二进制改为开放的 `Decimal128(38, 16)`。**v1 的 Bar/Quote/Trade Arrow schema 用 `FixedSizeBinary` 存 raw 整数；v2 目录对所有价格和数量列统一用 `Decimal128(38, 16)`，每个文件的 schema 元数据另存 `price_precision`/`size_precision`，并要求同一文件内精度一致。官方文档说这样 PyArrow、pandas、Polars 不需要 Nautilus 专用解码器就能读。内存模型仍是 raw 整数（Python wheel 为 128 位、16 位小数）。（事实，[NT-N1]–[NT-N5]）LEAN 内存一律用 C# `decimal`，磁盘是 zip 里的 CSV 文本：加密货币写原值，美股用“乘 10000 的整数”再在读时乘 `1/10000m`。（事实，[L-N1]–[L-N3]）
4. **“精确对象 + 表格”的双重返回，先例的做法都是两条入口、表格默认偏 float。**Nautilus 的 `query_bars` 返回 `list[Bar]`（`Price`/`Quantity` 定点对象），另有 `query_catalog(..., display=True)` 默认返回 float64 的“展示视图”，`display=False` 才给精确 `Decimal128` 列（pandas 下变成 `decimal.Decimal` 对象列，或 `ArrowDtype`）。LEAN 的 `self.history[TradeBar](...)` 返回 `TradeBar` 对象，而 DataFrame 形态在转换时把 `decimal` 统一转成 `double`。OpenBB 的标准模型本身就把 OHLC 声明为 `float`，`OBBject.to_df()/to_polars()/to_numpy()` 都从同一份 pydantic 结果派生。（事实，[NT-S1]–[NT-S3]、[L-S1]–[L-S3]、[O-1]、[O-2]）
5. **本机实测（100 万行 1m K 线，zstd）：`decimal128(38,18)` 文件最大、转 Python `Decimal` 最慢，而且在 DuckDB 里直接相乘会溢出。**文件 41.1 MB（int64 定点 20.1 MB、字符串 24.1 MB）；读成 pyarrow Table 约 21 ms（int64 约 11 ms）；500 万个值转 `Decimal` 约 1.41 s（字符串 0.39 s、int64 0.60 s）。`decimal128(38,18)` 的整数部分最多 20 位，能装下 1e-18 到约 1e20 的范围，0.0xx 价格和 1e11 级成交量都没问题；但读回带 18 位尾零，`normalize()` 会把 `95000.0` 变成 `9.5E+4`，要按标的精度 `quantize`。DuckDB 里 `close*volume` 的结果类型是 `DECIMAL(38,36)`，1000BONK 段的普通数值就报溢出。另测了一个折中：每个标的一个文件、`decimal128(18, 标的精度)` 加 `store_decimal_as_integer=True`，Parquet 物理类型是 INT64，文件 21.9 MB，读 11 ms，转 float 11 ms，读回直接是标的精度、没有尾零。（实测，附录 A）

## 2. 历史读取路径

### 2.1 各项目怎么读（事实）

| 项目 | 研究环境/策略读历史的路径 | 是否经服务 | 来源 |
|---|---|---|---|
| NautilusTrader | `ParquetDataCatalog` 是 Rust 目录与 DataFusion 查询引擎的 Python 接口，本地路径或对象存储 URI 决定后端，“不需要单独的数据库服务”。研究用 `query_*`；回测用 `BacktestDataConfig` 读同一目录；实盘节点用 `LiveNodeConfig.catalogs` 注册目录供历史请求使用（不预加载） | 否，进程内直读 Parquet | [NT-C1]、[NT-C2]、[NT-C3] |
| LEAN | 回测装配 `SubscriptionDataReaderHistoryProvider`（读本地数据文件）；实盘装配 `[BrokerageHistoryProvider, SubscriptionDataReaderHistoryProvider]`；`HistoryProviderManager` 依次调用各 provider，按时间同步，同一时刻的 Slice 合并。QuantBook 在 Notebook 进程里装配同一套 provider（见已有调研） | 否，进程内读本地文件；实盘可加券商历史接口 | [L-H1]、[L-H2] |
| ArcticDB | “无服务器的 DataFrame 数据库”，客户端直接从存储（S3/Azure/LMDB 等）拉压缩数据；`read(symbol, as_of=..., date_range=...)` 在客户端执行。单个 symbol 不支持并发写，同 symbol 并发写是“最后写者胜”，除非用 `stage` + `finalize_staged_data` | 否 | [A-1]、[A-2]、[A-3] |
| OpenBB Platform | `from openbb import obb` 在进程内调用 provider（多数是远端 API），返回 `OBBject`；`openbb-api` 把已安装命令用 FastAPI/Uvicorn 服务在 `127.0.0.1:6900`，主要用于 OpenBB Workspace 和 REST 访问。它没有本地历史库 | Python 用法不经服务；REST 是可选的另一入口 | [O-3]、[O-4] |

### 2.2 “已落盘 + 最新部分”的拼接

**Nautilus 实盘（事实）。**

- 历史请求：`dispatch_date_range_request` 先把请求区间限定到“当前时钟之前”，然后用目录的覆盖区间算出缺口（`catalog_coverage_for_request`），生成计划：一个 `RequestLeg::Catalog`（目录已覆盖部分）+ 若干 `RequestLeg::Client`（缺口区间交给交易所数据客户端）。`dispatch_request_plan` 把各段注册到同一个父请求的 pipeline 下。参数 `skip_catalog_data` 可以让客户端负责整段。[NT-E1]
- 回写：`params={"update_catalog": True}` 时才把客户端的成功响应写回目录；有 `limit` 的请求、源 schema 不明的请求会被拒绝回写，因为“解码出的行数不能证明源已经穷尽”。[NT-C2]
- 订阅：单标的订阅如果没有给 `start_ns`，DataEngine 会查目录里该标的的最后时间戳，预填到订阅参数里（`subscribe_command_with_prefilled_start_ns`）。客户端是否使用由适配器决定，本快照里在 Interactive Brokers 适配器看到读取该参数。[NT-E2]
- 实时数据进内存 `Cache`，每个 bar 类型最多保留 `bar_capacity` 根（默认 10,000），满了淘汰最旧；`cache.bars(bar_type)` 按 `ts_event` 降序返回。策略里“最近 N 根”通常来自 Cache，而不是目录。[NT-E3]
- 实时写盘走 Feather 流式文件，`promote` 后才在目录可见（已有调研 [NT9c]）。

**LEAN 实盘（事实）。**

- 实时订阅创建时先建 `LastPointTracker`，注释写明：从文件枚举器拿到的最后一个点开始做历史枚举，这样重复点会被过滤器立刻丢弃，避免内存尖峰。[L-W1]
- 预热（warm-up）：`GetWarmupEnumerator` 把“本地文件预热枚举器”和“历史 provider 预热枚举器”用 `ConcatEnumerator` 串起来，**丢弃重复时间戳**；历史部分的起点受 `maximum-warmup-history-days-look-back`（默认 5 天）限制；用 `FilterEnumerator` 挡住 `EndTime > 预热截止时间` 的数据；最后再 `ConcatEnumerator(预热, 实时)`。[L-W2]
- 普通 `History()` 调用在实盘里经 `HistoryProviderManager` 同时问券商历史接口和本地文件，按时间同步，同一时刻的 Slice 用 `MergeSlice` 合并。[L-H2]

**ArcticDB（事实）。**它把“最新部分”也视为同一个 symbol 的新版本：`append`/`update` 产生新版本，`stage` 写的段“在 finalize 之前不可读”。`as_of` 可以是版本号、快照名或 `datetime`，语义是“该时刻存在的那个版本”，也就是写入时间，不是信息可得时间。[A-2]、[A-3]

**推断。**两个框架拼接时都依赖“时间戳单调 + 边界去重”这个约定，没有任何一方提供 `ts_available <= as_of` 这样的信息可得时间截断：Nautilus Python 的 `query_catalog` 对 Parquet 目录直接拒绝 `as_of`（`ValueError("as_of is not supported by ParquetDataCatalog")`）[NT-S2]；ArcticDB 的 `as_of` 是版本时间。Talaria 的防前视截断必须自己做，并且在“直读文件”和“经服务补尾”两条路径上用同一段代码。

## 3. 数值类型

### 3.1 NautilusTrader：内存定点、目录 `Decimal128(38, 16)`（事实）

| 项 | 内容 | 来源 |
|---|---|---|
| 内存表示 | `Price`、`Quantity`、`Money` 是定点类型，raw 整数 × 全局刻度。高精度模式：`Price`/`Money` 为 `i128`，`Quantity` 为 `u128`，最大精度 16；标准模式：`i64`/`u64`，最大精度 9。官方 Python wheel 在所有平台都用高精度模式 | [NT-N1] |
| 刻度 | 全局刻度标准模式 10^9、高精度模式 10^16（`FIXED_PRECISION`）。`precision` 字段只控制显示和序列化，相等比较按数值：`Price(1.23, 2) == Price(1.230, 3)` | [NT-N2] |
| raw 合法性 | 精度低于 `FIXED_PRECISION` 时，raw 必须能被 `10^(FIXED_PRECISION - precision)` 整除，否则 Python 抛 `ValueError`；`from_decimal()` 超出位数按银行家舍入 | [NT-N3] |
| 目录存储（v2） | 价格和数量列统一 `Decimal128(38, 16)`（`FIXED_DECIMAL_PRECISION = 38`，`FIXED_DECIMAL_SCALE = 16`），Bar 的 `open/high/low/close/volume` 都用它；文件元数据存 `price_precision`/`size_precision`；精度超过 16 的写入直接报错（所以 DeFi 的 wei 精度不在目录表示范围内） | [NT-N4] |
| 目录存储（v1） | 1.211.0 起 Bar/Quote/Trade 等 Arrow schema 改用 `FixedSizeBinary` 以支持两种精度模式；v2 的迁移工具把 8/16 字节定宽字段“规范化为十进制” | [NT-N5] |
| 迁移不舍入 | 旧目录里位数超过声明精度的行，迁移时失败而不是舍入；旧写入器 `int(value * FIXED_SCALAR)` 可能引入浮点误差，需要重新生成 | [NT-N3]、[NT-N5] |
| 精度变化 | tick size 变化导致精度变化时，前后数据不能合并到同一个文件 | [NT-N2] |
| 转 float / Decimal | `as_decimal()` 不经二进制浮点；`as_double()`/`float()` 在精度 ≤16 时可能舍入，精度 17/18 直接报错。官方提醒 `as_decimal()` 用 Rust `Decimal`（96 位尾数），有效位过多时可能丢小数位 | [NT-N6] |
| 开放格式的理由 | 迁移文档：“PyArrow、pandas 和 Polars 不需要 Nautilus 专用的二进制解码器就能读取开放的 Arrow 值” | [NT-N4]（作者宣称） |

### 3.2 LEAN：内存 C# `decimal`，磁盘文本（事实）

- `TradeBar` 的 `Open/High/Low/Close/Volume` 都是 C# `decimal`。[L-N1]
- C# `decimal` 是 16 字节、28–29 位有效数字、96 位整数尾数乘 10 的幂，不支持 NaN/无穷；官方说明它适合“精度由小数点右边位数决定”的金融场景，并提到为了性能或存储也可以用 `double`/`float`。[L-N4]
- 磁盘：zip 包里一个 CSV。加密货币分钟/秒级字段直接写十进制价格（示例 `132.01`）[L-N2]，解析走 `ParseCrypto → LineParseNoScale`，不乘刻度 [L-N1]；美股价格存“deci-cents”（乘 10000 的整数），读时乘 `_scaleFactor = 1/10000m` [L-N1]、[L-N3]。也就是说 LEAN 磁盘上同时有“十进制文本”和“定点整数文本”两种做法。
- DataFrame：`PandasData` 往序列里加值时 `input is decimal ? Convert.ToDouble(...)`，所以 `History()` 的 DataFrame 列是 float64。[L-S2]

### 3.3 ArcticDB：不原生支持 decimal（事实）

- FAQ 列出的原生类型只有 DataFrame/ndarray、整数、浮点、布尔、字符串，其他类型要 pickle。[A-4]
- Arrow 写入路径的类型映射只接受 bool、各宽度整数、float32/64、纳秒时间戳、string/large_string，其他类型抛 `E_UNSUPPORTED_COLUMN_TYPE`。[A-5]
- 读取可选 `OutputFormat.PANDAS`（默认，numpy 支撑）、`PYARROW`、`POLARS`。[A-6]
- 许可证是 BSL 1.1，限制把它作为“数据库服务”商业提供给第三方。[A-7]

### 3.4 Arrow / Parquet / pandas / Polars / DuckDB 的 decimal 支持

| 项 | 内容 | 类别 | 来源 |
|---|---|---|---|
| Arrow `decimal128` | 定点十进制，按缩放整数存；精度必须在 1–38，超过 38 位用 `decimal256`；scale 可以为负 | 事实 | [S-1] |
| Arrow 内存布局 | Decimal128 值占 16 字节定宽 | 事实 | [S-2] |
| Parquet DECIMAL | 可以标注 `int32`（精度 1–9）、`int64`（1–18）、`fixed_len_byte_array`（精度受字节数限制）、`byte_array` | 事实 | [S-3] |
| PyArrow 写 Parquet | `store_decimal_as_integer` 默认 `False`，此时所有 decimal 都标注为 `fixed_len_byte_array`；开启后精度 1–18 的 decimal 可存成整数 | 事实 | [S-4] |
| PyArrow 写入行为 | `decimal128(38,18)` 写出的物理类型是 `FIXED_LEN_BYTE_ARRAY`；`decimal128(18,7)` + `store_decimal_as_integer=True` 写出 `INT64` 加 `Decimal(18,7)` 逻辑类型 | 实测 | 附录 A.3 |
| `Table.to_pandas()` 的 decimal 列 | 官方 Arrow→pandas 类型表没有 decimal 行；实测默认得到 `object` 列，元素为 `decimal.Decimal` | 实测（官方未写） | [S-5]、附录 A.4 |
| pandas `ArrowDtype` | 官方用 `pd.ArrowDtype(pa.decimal128(3, scale=2))` 举例；`to_pandas(types_mapper=pd.ArrowDtype)` 保留 Arrow 类型 | 事实 | [S-6] |
| pandas 运算 | `ArrowDtype` decimal 列 `sum/mean` 返回 `Decimal`；`close*volume`（38,18 × 38,18）报 “precision out of range: 77”；`rolling(20).mean()` 结果变成 float64。object 列 `rolling().mean()` 也变 float64 | 实测 | 附录 A.4 |
| Polars `Decimal` | 128 位十进制；精度默认 38，“Polars 支持的最大值”。文档没有标注不稳定 | 事实 | [S-7] |
| Polars 运算 | 读为 `Decimal(38,18)`；`to_list()` 给 `Decimal`；`sum` 给 `Decimal`；`mean` 和 `rolling_mean` 给 float；`close*volume` 结果仍是 `Decimal(38,18)`，本例数值与 Python 精确积一致 | 实测 | 附录 A.4、A.6 |
| DuckDB DECIMAL | 宽度 1–38；按宽度内部存 INT16/32/64/128；宽度超过 19 的运算“慢得多”，建议 ≤18；加减乘返回能容纳精确结果的类型，所需宽度超过 38 就报错 | 事实 | [S-8] |
| DuckDB 运算 | `DECIMAL(38,18) * DECIMAL(38,18)` 结果类型 `DECIMAL(38,36)`，1000BONK 段（0.0234 × 3.5e9）直接 `Overflow in multiplication`；先 CAST 到 `DECIMAL(38,9)` 就正常 | 实测 | 附录 A.4、A.6 |
| Python `Decimal` | 新建 `Decimal` 的有效位只由输入位数决定，上下文精度只在运算时起作用；默认精度 28；`normalize()` 去掉所有尾零（`200` 变 `2E+2`）；`quantize()` 返回取第二个操作数指数的值 | 事实 | [S-9] |

### 3.5 本机基准（实测）

机器与版本：Apple M4，16 GB 内存，macOS 27.2.1（arm64）；Python 3.15.2；pyarrow 26.0.1、polars 2.1.0、pandas 3.1.1、duckdb 2.0.1、numpy 2.5.5；uv 0.13.9。

数据：100 万行 1m K 线，一半 `BTC-USDT-PERP`（价格约 95000，1 位小数；成交量 3 位小数）、一半 `1000BONK-USDT-PERP`（价格约 0.0234，7 位小数；成交量为整数，单根约 1e9–1e11）。每行 `symbol`（字典编码）、`ts_event`（int64）和 5 个数值列 `open/high/low/close/volume`。所有文件 zstd 压缩、单文件写出。计时在 `gc.disable()` 下进行，“读”取 7 次、“转 Decimal”取 5 次的最小值和中位数。“转 Decimal”是 5 列共 500 万个值转成 Python `decimal.Decimal` 列表；“转 float”是 5 列转 numpy float64。

| 存储表示 | 文件大小 | 读成 pyarrow Table（min / 中位） | 转 Python `Decimal`（min / 中位） | 转 float numpy（min / 中位） | 读回数值与原值相等 |
|---|---|---|---|---|---|
| `decimal128(38,18)` | 41.1 MB | 21.1 / 21.9 ms | 1.41 / 1.41 s | 67.5 / 68.5 ms | 是（带 18 位尾零） |
| `decimal128(38,16)`（Nautilus 同款） | 39.1 MB | 21.8 / 23.4 ms | 1.37 / 1.37 s | 47.0 / 48.2 ms | 是（带 16 位尾零） |
| int64 定点 + 每行 `price_scale`/`size_scale`（int8） | 20.1 MB | 10.7 / 11.9 ms | 0.60 / 0.60 s（`Decimal(int).scaleb(-s)`） | 24.6 / 25.2 ms | 是（按标的精度，无多余尾零） |
| string（现状） | 24.1 MB | 19.5 / 21.7 ms | 0.39 / 0.40 s（`Decimal(str)`） | 41.9 / 44.8 ms（`pc.cast`） | 是 |
| float64（仅作参照，非精确） | 31.3 MB | 10.4 / 11.8 ms | 0.99 / 1.00 s（`Decimal(repr(x))`） | 5.4 / 5.5 ms | 本数据集是 |
| `decimal128(18, 标的精度)`，按标的分两个文件，`store_decimal_as_integer=True` | 21.9 MB（无 symbol 列） | 10.8 / 11.4 ms | 1.14 / 1.15 s | 10.5 / 11.0 ms | 是（按标的精度，无多余尾零） |

参照：在 50 万个 `Decimal` 收盘价上跑一遍纯 Python 滑动 SMA20 用 0.13 s。也就是说，对 ADR-0010 的算子来说，“把存储读成 `Decimal`”的成本与算一个简单指标同量级，甚至更高。

关键观察（实测）：

1. **`decimal128` 转 Python `Decimal` 是最慢的路径。**`to_pylist()` 每个值约 0.28 µs，比从字符串 `Decimal(str)` 慢约 3.5 倍，比从 int64 尾数构造慢约 2.3 倍。所以 decimal128 的优势在“列式、类型自描述、可在 Arrow/Polars/DuckDB 里做向量运算”，而不在“喂给 Python `Decimal` 算子”。
2. **int64 定点的文件最小、读最快。**统一 scale 18 的 int64 只能表示到约 9.22，不可行；按标的精度缩放才行：BTC（1 位小数）上限约 9.2e17，1000BONK（7 位小数）上限约 9.2e11。缺点是 scale 不在类型里：要么每行带 scale 列，要么写进文件元数据，读者必须懂这个约定，外部工具（DuckDB、Polars）看到的是没有意义的整数。
3. **`decimal128(18, 标的精度)` + `store_decimal_as_integer` 兼得一部分优点。**物理上是 INT64，体积接近 int64 定点；逻辑类型自带 scale，pyarrow/Polars/DuckDB 都直接读成 `decimal(18,7)`；读回就是标的精度，不需要 `quantize`。代价是同一列在一个文件里只能有一个 scale，所以只能按标的（且按精度不变的时段）分文件，这与 Nautilus “精度变化时不能合并文件”的约束一样 [NT-N2]。Talaria 冷库本来就按 `<venue>/<symbol>/<timeframe>` 分目录。
4. **字符串现状并不差。**体积第二小，转 `Decimal` 最快；缺点是不能在 Arrow/Polars/DuckDB 里直接做数值过滤和聚合，每次都要 `CAST`，而且列里可以混入不合法文本，类型不自我约束。

### 3.6 边界值核对（实测，附录 A.1–A.2）

| 用例 | `decimal128(38,18)` 结果 |
|---|---|
| 价格 1e-18 | 正常，读回 `Decimal('1E-18')` |
| 价格 1e-19（超出 scale） | `ArrowInvalid: Rescaling Decimal value would cause data loss`，写入失败而不是静默舍入 |
| 1000BONK 价格 `0.0234567` | 读回 `Decimal('0.023456700000000000')` |
| 成交量 `99999999999999999999.999999999999999999`（20 位整数） | 正常 |
| 成交量 `1e20`（21 位整数） | 报错，写入失败 |
| 直接传 Python float `0.1` | `ArrowTypeError`：只接受 int 或 `Decimal` |
| `pc.cast(float64 0.1 → decimal128(38,18))` | 得到 `0.100000000000000006`，把二进制误差固化进去了 |
| 读回 `95000.0` / `1200` | `95000.000000000000000000` / `1200.000000000000000000`；`normalize()` 变成 `9.5E+4` / `1.2E+3`；按标的指数 `quantize` 得 `95000.0` / `1200` |
| 相等与哈希 | 与 `Decimal('95000.0')` 相等且哈希相同，尾零只影响字符串和显示 |
| 38 位有效数字的值 | 读回保留 38 位；一做加法就按默认上下文精度 28 舍入 |

结论（推断）：`decimal128(38,18)` 对加密货币的价格和单根成交量**不会溢出**，范围约 1e-18 到 1e20；风险在后续运算：Arrow/pandas 的乘法精度超过 38 会报错，DuckDB 乘法会溢出，Python 运算超过 28 位有效数字会舍入。尾零可以用 `quantize(标的指数)` 处理，不要用 `normalize()`，它会产生科学计数法字符串，影响黄金向量比对和日志可读性。

## 4. SDK 返回形态的先例

| 项目 | 精确对象（给策略） | 表格（给 Notebook） | 数值类型 | 来源 |
|---|---|---|---|---|
| NautilusTrader | `catalog.query_bars(...) -> list[Bar]`；策略在 `on_bar` 里收到 `Bar`，从 `Cache.bars()` 取最近 N 根 | `query_catalog(catalog, data_type, output=PANDAS/POLARS/ARROW, display=True, use_arrow_dtypes=False)`：`display=True`（默认）是 float64 的“展示视图”，`display=False` 是精确的 `Decimal128(38,16)` 和全部存储列；pandas 下 decimal 列变成 `decimal.Decimal` 对象，开 `use_arrow_dtypes` 则用 `pd.ArrowDtype`。另有 `query_data_arrow_stream(...)` 给 Arrow IPC 流 | 对象内是定点 raw；表格默认 float | [NT-S1]、[NT-S2]、[NT-S3]、[NT-E3] |
| LEAN | `self.history[TradeBar](symbol, n, ...)` 返回 `TradeBar` 序列；C# 侧 `History<T>` 泛型重载 | `self.history(tickers, periods, ...)` 返回 pandas DataFrame（多级索引），转换时 `decimal → double` | 对象内 `decimal`；表格 float64 | [L-S1]、[L-S2]、[L-S3] |
| ArcticDB | 无对象模型，只有 DataFrame/Arrow | `read(..., output_format=PANDAS/PYARROW/POLARS)` | 不支持 decimal | [A-4]、[A-6] |
| OpenBB | `OBBject.results` 是 pydantic 模型列表（如 `EquityHistoricalData`，`open/high/low/close: float`） | `to_df()`（`to_dataframe` 的别名）、`to_polars()`（先转 pandas 再 `pl.from_pandas`）、`to_numpy()`、`to_dict()`、`to_llm()` | 全程 float | [O-1]、[O-2] |

共同点（推断）：

- 都是“一次查询，两个出口”，而不是两套数据源。精确出口给逐条处理的策略，表格出口给向量化分析和画图。
- 表格出口默认偏向 float。Nautilus 甚至把精确表格放在非默认的 `display=False` 后面；LEAN 和 OpenBB 的表格只有 float。原因是 pandas 的 object 列和 decimal 扩展类型在常用运算上要么慢、要么静默转 float（见 3.4 的 rolling），float 才是 Notebook 生态的默认。
- 精确出口都附带“精度”元数据：Nautilus 在对象里存 `precision`、在文件元数据里存 `price_precision`；LEAN 的 `decimal` 自身保留小数位数（例如 `1.50m`）。

## 5. 对 Talaria 的启示

以下都是**推断**，只列可选方案与取舍，最终由用户决定。三个子项相互影响：存储表示决定了 SDK 转换成本，读取路径决定了 `ts_available` 截断放在哪。

### 5.1 历史读取路径

| 方案 | 做法 | 好处 | 代价/风险 |
|---|---|---|---|
| A. SDK 直读已发布 Parquet，服务只补“热尾” | SDK 在调用方进程打开 SQLite 清单（只读），选出覆盖区间的 generation，直读 Parquet，在同一段代码里做 `ts_available <= as_of` 截断；区间末端超过清单最后一个已提交时间时，再向常驻服务的 HTTP/WS 要“尚未冻结的已收盘 K 线”，按 `(symbol, timeframe, ts_event)` 去重拼接（LEAN `ConcatEnumerator` 去重、Nautilus 缺口补请求的思路） | 回测几十万到上千万行不经序列化，速度取决于本机磁盘；服务停了历史仍可读；与 D2“回测直读 Parquet”一致 | SDK 要懂清单 schema，清单格式变更会和 SDK 版本耦合；拼接边界（尚未冻结的那段）需要明确语义：服务返回的行也必须带 `ts_available`，截断规则两边一致；要防止读到 staging 或被替换的 generation（清单是唯一可见性来源） |
| B. 全部经服务 | SDK 只调服务接口，服务读 Parquet + 内存热尾，返回 Arrow IPC 流 | 单一实现，清单和存储格式可以随意改；截断和拼接只在服务里写一次 | 千万行回测要经本机序列化和传输；服务成为回测和 Notebook 的单点；与 D2 已定的“回测直读、不经服务”冲突，要重议 |
| C. 混合：回测直读，Notebook 与实时经服务 | 回测引擎直读文件（只读冷数据，不需要热尾）；Notebook/策略的 `bars(limit=500)` 这类“最近 N 根”经服务 | 大批量走最快路径，小批量拿到最新数据且不用在 SDK 里拼接 | 两条路径要保证同口径（同一截断函数、同一 schema 映射），否则出现“Notebook 和回测数值不同”；需要回归测试对比两条路径 |

需要额外确认的点：

- 服务的“热尾”是否已有 `ts_available`：如果服务内存里的最新 K 线没有可得时间，方案 A/C 都无法统一截断。
- 清单的只读打开方式要与 ADR-0012（状态库专用写线程 + 只读连接）对齐。

### 5.2 SDK 默认返回类型

| 方案 | 做法 | 好处 | 代价/风险 |
|---|---|---|---|
| a. 表格默认 float，精确需显式（Nautilus `display=True` 式） | `c.bars(...)` 返回 pandas/Polars float64；`c.bars(..., exact=True)` 或 `c.bar_objects(...)` 给 `Decimal` | Notebook 体验最好，与 pandas/绘图生态一致；转换最快（int64 或 decimal(18,s) 转 float 约 10–25 ms/百万行×5 列） | 与 ADR-0010“Decimal 唯一权威”有张力：用户在 Notebook 里用 float 自算的指标可能和权威算子差最后几位；需要文档和类型上提醒 |
| b. 默认精确（`Decimal` 或 decimal 列） | `c.bars(...)` 返回 pyarrow Table / Polars（decimal 列）或 `list[Bar]`（`Decimal` 字段）；`.to_float()` 显式转换 | 与 ADR-0010 一致，策略与 Notebook 数值一致 | pandas 下是 object 列或 `ArrowDtype`，常用运算会静默转 float 或报精度错误（3.4）；100 万行转 `Decimal` 需 0.4–1.4 s |
| c. 结果对象，多个出口（OpenBB `OBBject` 式） | `c.bars(...)` 返回一个结果对象，内部持有 Arrow Table（精确列 + 精度元数据）；提供 `.to_pandas()`（float）、`.to_polars()`、`.to_arrow()`、`.to_decimals()`/`.iter_bars()`（`Decimal` 对象） | 一次查询、多种形态，延迟转换（不用的形态不付费）；精确和 float 都显式可见 | 多一层包装类型，需要设计 repr 和文档；`c.bars(...)` 不能直接当 DataFrame 用，Notebook 里多写一个 `.to_pandas()` |

与 ADR-0010 的衔接：无论哪种方案，指标都应由权威算子从 `Decimal` 序列计算，SDK 的 float 表格只用于浏览和画图；如果要在 Notebook 里看指标，提供“调权威算子”的入口，而不是让用户在 pandas 里重写。

### 5.3 存储数值表示

| 方案 | 精度与范围 | 文件大小（实测） | 读 / 转 Decimal / 转 float（实测） | 与 Decimal 算子衔接 | 主要风险 |
|---|---|---|---|---|---|
| 字符串（现状） | 任意，靠写入方保证格式 | 24.1 MB | 20 ms / 0.39 s / 42 ms | 最直接，`Decimal(str)` 最快 | 类型不自约束；外部查询每次都要 CAST；数值过滤和聚合不能下推 |
| `decimal128(38,18)` | 约 1e-18 至 1e20，超出就写入失败 | 41.1 MB | 21 ms / 1.41 s / 68 ms | 读回带 18 位尾零，需要按标的指数 `quantize` | 文件最大；转 `Decimal` 最慢；DuckDB/pandas 乘法会溢出或报精度错误；38 位值在 Python 默认精度 28 下运算会舍入 |
| `decimal128(38,16)`（Nautilus 同款） | 约 1e-16 至 1e22 | 39.1 MB | 22 ms / 1.37 s / 47 ms | 同上，16 位尾零 | 同上；好处是与 Nautilus v2 目录格式一致，便于以后互通 |
| int64 定点（按标的精度），scale 存列或元数据 | 每标的上限 9.22e18 / 10^scale | 20.1 MB | 11 ms / 0.60 s / 25 ms | 构造 `Decimal(int).scaleb(-s)` 精确无尾零 | scale 不在类型里，外部工具看到的是裸整数；读者必须遵守约定；精度变化要换文件或带每行 scale |
| `decimal128(18, 标的精度)` + `store_decimal_as_integer`，按标的分文件 | 每标的上限约 1e18 / 10^scale（18 位有效数字） | 21.9 MB | 11 ms / 1.14 s / 11 ms | 读回就是标的精度，无尾零 | 18 位有效数字的上限对“价格位数 + 成交额”是否够用要逐标的核对；tick size 变化必须切新文件；成交额（turnover）可能需要更大的精度或单独的 scale |

补充判断：

- 如果 SDK 默认给 float 表格（5.2 方案 a），存储选能快速转 float 的类型更划算：int64 定点或 `decimal(18,s)`。如果 SDK 默认给 `Decimal`（方案 b），字符串的转换反而最快，decimal128 的优势只剩“类型自描述和外部可查询”。
- 不论选哪种，写入路径都应拒绝从 float 构造 decimal（3.6 里 `pc.cast(float→decimal)` 把 `0.1` 固化成 `0.100000000000000006`），交易所原始文本应直接解析成 `Decimal` 或整数尾数。
- 若改表示，需要一次冷库迁移；Nautilus 的经验是迁移“不舍入”、超位数的行直接失败，并保留源目录以便回滚 [NT-N3]、[NT-N5]。

## 6. 没有查到一手资料的问题

- PyArrow 官方文档的 Arrow→pandas 类型表没有 decimal 行，`to_pandas()` 默认把 decimal 转成 `decimal.Decimal` 对象列只是实测结果。[S-5]
- LEAN Python 算法里通过 pythonnet 访问 `TradeBar.Close`（C# `decimal`）时得到的 Python 类型，本次没有在 LEAN 源码或官方文档中找到明确说明。
- Nautilus 从 `FixedSizeBinary` 改为 `Decimal128(38, 16)` 的具体版本与动机：`RELEASES.md` 没有对应条目，只能从 v2 迁移文档和源码确认现状。
- Polars `Decimal` 的稳定性：2.1.0 文档没有标为不稳定，也没有说明运算结果的精度推导规则；本次乘法结果保持 `Decimal(38,18)` 是实测。
- ArcticDB 是否计划支持 decimal：本次未查 issue 和路线图。
- 未测：并发读写下 SDK 直读与服务补尾的一致性；Arrow IPC 经本机 HTTP 传输千万行的耗时。这两项会影响 5.1 的选择，建议在原型里实测。

## 7. 来源

### NautilusTrader（提交 `0db3667a`）

- [NT-N1] [docs/concepts/overview.md L241–272：定点值类型、高精度与标准模式、官方 wheel 用高精度](https://github.com/nautechsystems/nautilus_trader/blob/0db3667a3c7b13e01f12a9bf446616c571f1fce0/docs/concepts/overview.md#L241-L272)
- [NT-N2] [docs/concepts/value_types.md L162–195：precision 字段、全局刻度、相等语义、按文件存精度、精度变化不能合并文件](https://github.com/nautechsystems/nautilus_trader/blob/0db3667a3c7b13e01f12a9bf446616c571f1fce0/docs/concepts/value_types.md#L162-L195)
- [NT-N3] [docs/concepts/data/index.md L563–605：raw 合法性、目录用 Decimal128(38,16)、from_decimal 舍入、旧 raw 迁移](https://github.com/nautechsystems/nautilus_trader/blob/0db3667a3c7b13e01f12a9bf446616c571f1fce0/docs/concepts/data/index.md#L563-L605)
- [NT-N4] [docs/how_to/migrate_parquet_catalog.md L46–80：当前 Arrow 表示、存储刻度 16 与元数据精度](https://github.com/nautechsystems/nautilus_trader/blob/0db3667a3c7b13e01f12a9bf446616c571f1fce0/docs/how_to/migrate_parquet_catalog.md#L46-L80)；[L236–240：PyArrow/pandas/Polars 无需专用解码器](https://github.com/nautechsystems/nautilus_trader/blob/0db3667a3c7b13e01f12a9bf446616c571f1fce0/docs/how_to/migrate_parquet_catalog.md#L236-L240)；[crates/serialization/src/arrow/mod.rs L127–128、L244–260、L566–571：FIXED_DECIMAL_PRECISION/SCALE、拒绝超过 16 的精度](https://github.com/nautechsystems/nautilus_trader/blob/0db3667a3c7b13e01f12a9bf446616c571f1fce0/crates/serialization/src/arrow/mod.rs#L127-L128)；[crates/serialization/src/arrow/bar.rs L35–48：Bar schema](https://github.com/nautechsystems/nautilus_trader/blob/0db3667a3c7b13e01f12a9bf446616c571f1fce0/crates/serialization/src/arrow/bar.rs#L35-L48)
- [NT-N5] [RELEASES.md L6486、L6534–6538：1.211.0 改用 FixedSizeBinary](https://github.com/nautechsystems/nautilus_trader/blob/0db3667a3c7b13e01f12a9bf446616c571f1fce0/RELEASES.md#L6534-L6538)；[MIGRATION_V2.md L913–926：v2 schema 不同、迁移不舍入](https://github.com/nautechsystems/nautilus_trader/blob/0db3667a3c7b13e01f12a9bf446616c571f1fce0/MIGRATION_V2.md#L913-L926)；[crates/serialization/src/arrow/legacy.rs L711、L784–830：8/16 字节定宽字段转十进制](https://github.com/nautechsystems/nautilus_trader/blob/0db3667a3c7b13e01f12a9bf446616c571f1fce0/crates/serialization/src/arrow/legacy.rs#L711)
- [NT-N6] [docs/concepts/value_types.md L305–325：as_decimal / as_double 的精度说明](https://github.com/nautechsystems/nautilus_trader/blob/0db3667a3c7b13e01f12a9bf446616c571f1fce0/docs/concepts/value_types.md#L305-L325)
- [NT-C1] [docs/concepts/data/catalog.md L1–18：Rust 目录 + DataFusion，不需要单独数据库服务](https://github.com/nautechsystems/nautilus_trader/blob/0db3667a3c7b13e01f12a9bf446616c571f1fce0/docs/concepts/data/catalog.md#L1-L18)
- [NT-C2] [docs/concepts/data/catalog.md L196–242：typed query、LiveNodeConfig.catalogs 缺口请求、回写规则](https://github.com/nautechsystems/nautilus_trader/blob/0db3667a3c7b13e01f12a9bf446616c571f1fce0/docs/concepts/data/catalog.md#L196-L242)
- [NT-C3] [docs/concepts/data/catalog.md L400–430：三种接入方式、query 与 typed query](https://github.com/nautechsystems/nautilus_trader/blob/0db3667a3c7b13e01f12a9bf446616c571f1fce0/docs/concepts/data/catalog.md#L400-L430)
- [NT-E1] [crates/data/src/engine/streaming.rs L417–515：按目录覆盖区间拆成目录段 + 客户端缺口段](https://github.com/nautechsystems/nautilus_trader/blob/0db3667a3c7b13e01f12a9bf446616c571f1fce0/crates/data/src/engine/streaming.rs#L417-L515)；[L630–685：dispatch_request_plan](https://github.com/nautechsystems/nautilus_trader/blob/0db3667a3c7b13e01f12a9bf446616c571f1fce0/crates/data/src/engine/streaming.rs#L630-L685)
- [NT-E2] [crates/data/src/engine/streaming.rs L69–180：订阅时用目录最后时间戳预填 start_ns](https://github.com/nautechsystems/nautilus_trader/blob/0db3667a3c7b13e01f12a9bf446616c571f1fce0/crates/data/src/engine/streaming.rs#L69-L180)；[crates/adapters/interactive_brokers/src/data/core.rs L189–198：适配器读取 start_ns](https://github.com/nautechsystems/nautilus_trader/blob/0db3667a3c7b13e01f12a9bf446616c571f1fce0/crates/adapters/interactive_brokers/src/data/core.rs#L189-L198)
- [NT-E3] [docs/concepts/cache.md L104–128：bar_capacity 默认 10,000、满时淘汰最旧](https://github.com/nautechsystems/nautilus_trader/blob/0db3667a3c7b13e01f12a9bf446616c571f1fce0/docs/concepts/cache.md#L104-L128)；[L350–377：bars() 降序、反向索引](https://github.com/nautechsystems/nautilus_trader/blob/0db3667a3c7b13e01f12a9bf446616c571f1fce0/docs/concepts/cache.md#L350-L377)
- [NT-S1] [python/nautilus_trader/persistence/\_\_init\_\_.pyi L454–460：query_bars 返回 list[Bar]](https://github.com/nautechsystems/nautilus_trader/blob/0db3667a3c7b13e01f12a9bf446616c571f1fce0/python/nautilus_trader/persistence/__init__.pyi#L454-L460)
- [NT-S2] [python/nautilus_trader/persistence/catalog_to_df.py L62–170：query_catalog 的 display / use_arrow_dtypes / as_of 拒绝](https://github.com/nautechsystems/nautilus_trader/blob/0db3667a3c7b13e01f12a9bf446616c571f1fce0/python/nautilus_trader/persistence/catalog_to_df.py#L62-L170)
- [NT-S3] [python/nautilus_trader/persistence/catalog_to_df.py L290–311：Arrow → pandas / ArrowDtype / Polars](https://github.com/nautechsystems/nautilus_trader/blob/0db3667a3c7b13e01f12a9bf446616c571f1fce0/python/nautilus_trader/persistence/catalog_to_df.py#L290-L311)

### LEAN（提交 `53236fa4`）

- [L-N1] [Common/Data/Market/TradeBar.cs L36–100：decimal 字段与 _scaleFactor = 1/10000m](https://github.com/QuantConnect/Lean/blob/53236fa4a23433a96c7f57cbe7850dba9be840ac/Common/Data/Market/TradeBar.cs#L36-L100)；[L432–443：ParseCrypto 不缩放](https://github.com/QuantConnect/Lean/blob/53236fa4a23433a96c7f57cbe7850dba9be840ac/Common/Data/Market/TradeBar.cs#L432-L443)；[L627–660：LineParseNoScale](https://github.com/QuantConnect/Lean/blob/53236fa4a23433a96c7f57cbe7850dba9be840ac/Common/Data/Market/TradeBar.cs#L627-L660)
- [L-N2] [Data/crypto/readme.md L30–46：加密分钟/秒级 CSV 格式](https://github.com/QuantConnect/Lean/blob/53236fa4a23433a96c7f57cbe7850dba9be840ac/Data/crypto/readme.md#L30-L46)
- [L-N3] [Data/equity/readme.md L70：价格除以 10,000 把 deci-cents 转成美元](https://github.com/QuantConnect/Lean/blob/53236fa4a23433a96c7f57cbe7850dba9be840ac/Data/equity/readme.md#L70)
- [L-N4] [Microsoft Learn：C# floating-point numeric types（decimal 16 字节、28–29 位、96 位尾数）](https://learn.microsoft.com/en-us/dotnet/csharp/language-reference/builtin-types/floating-point-numeric-types)
- [L-H1] [Launcher/config.json L386、L418：回测与实盘的 history-provider 装配](https://github.com/QuantConnect/Lean/blob/53236fa4a23433a96c7f57cbe7850dba9be840ac/Launcher/config.json#L386-L418)
- [L-H2] [Engine/HistoricalData/HistoryProviderManager.cs L135–206：依次调用 provider、同步并合并同时刻 Slice](https://github.com/QuantConnect/Lean/blob/53236fa4a23433a96c7f57cbe7850dba9be840ac/Engine/HistoricalData/HistoryProviderManager.cs#L135-L206)
- [L-W1] [Engine/DataFeeds/LiveTradingDataFeed.cs L201–207：LastPointTracker](https://github.com/QuantConnect/Lean/blob/53236fa4a23433a96c7f57cbe7850dba9be840ac/Engine/DataFeeds/LiveTradingDataFeed.cs#L201-L207)
- [L-W2] [Engine/DataFeeds/LiveTradingDataFeed.cs L420–470：文件 + 历史预热去重串接，再接实时](https://github.com/QuantConnect/Lean/blob/53236fa4a23433a96c7f57cbe7850dba9be840ac/Engine/DataFeeds/LiveTradingDataFeed.cs#L420-L470)；[L43：maximum-warmup-history-days-look-back 默认 5](https://github.com/QuantConnect/Lean/blob/53236fa4a23433a96c7f57cbe7850dba9be840ac/Engine/DataFeeds/LiveTradingDataFeed.cs#L43)
- [L-S1] [Algorithm/QCAlgorithm.History.cs L489、L521：History 返回 TradeBar / History\<T\>](https://github.com/QuantConnect/Lean/blob/53236fa4a23433a96c7f57cbe7850dba9be840ac/Algorithm/QCAlgorithm.History.cs#L489-L548)；[Algorithm.Python/ConsolidateHourBarsIntoDailyBarsRegressionAlgorithm.py L48：self.history\[TradeBar\]](https://github.com/QuantConnect/Lean/blob/53236fa4a23433a96c7f57cbe7850dba9be840ac/Algorithm.Python/ConsolidateHourBarsIntoDailyBarsRegressionAlgorithm.py#L48)
- [L-S2] [Common/Python/PandasData.cs L761–763：decimal 转 double](https://github.com/QuantConnect/Lean/blob/53236fa4a23433a96c7f57cbe7850dba9be840ac/Common/Python/PandasData.cs#L761-L763)
- [L-S3] [Algorithm/QCAlgorithm.Python.cs L1021–1066：Python History 返回 DataFrame](https://github.com/QuantConnect/Lean/blob/53236fa4a23433a96c7f57cbe7850dba9be840ac/Algorithm/QCAlgorithm.Python.cs#L1021-L1066)

### ArcticDB（提交 `0c1db332`）

- [A-1] [docs/mkdocs/docs/faq.md L28–55：与 Parquet 的区别、不需要服务器](https://github.com/man-group/ArcticDB/blob/0c1db33297e16007d501471173e5d36f1ed1845f/docs/mkdocs/docs/faq.md#L28-L55)；[README.md L23、L42：serverless、数据直接从存储拉到客户端](https://github.com/man-group/ArcticDB/blob/0c1db33297e16007d501471173e5d36f1ed1845f/README.md#L42)
- [A-2] [python/arcticdb/version_store/library.py L2111–2150：read 的 as_of 与 date_range](https://github.com/man-group/ArcticDB/blob/0c1db33297e16007d501471173e5d36f1ed1845f/python/arcticdb/version_store/library.py#L2111-L2150)
- [A-3] [python/arcticdb/version_store/library.py L1014–1050：stage 段在 finalize 前不可读](https://github.com/man-group/ArcticDB/blob/0c1db33297e16007d501471173e5d36f1ed1845f/python/arcticdb/version_store/library.py#L1014-L1050)；[faq.md L149–159：单 symbol 不支持并发写，最后写者胜](https://github.com/man-group/ArcticDB/blob/0c1db33297e16007d501471173e5d36f1ed1845f/docs/mkdocs/docs/faq.md#L149-L159)
- [A-4] [docs/mkdocs/docs/faq.md L115–127：原生支持的数据类型](https://github.com/man-group/ArcticDB/blob/0c1db33297e16007d501471173e5d36f1ed1845f/docs/mkdocs/docs/faq.md#L115-L127)
- [A-5] [cpp/arcticdb/arrow/arrow_utils.cpp L477–510：Arrow 输入类型映射，其他类型报错](https://github.com/man-group/ArcticDB/blob/0c1db33297e16007d501471173e5d36f1ed1845f/cpp/arcticdb/arrow/arrow_utils.cpp#L477-L510)
- [A-6] [python/arcticdb/options.py L192–217：OutputFormat](https://github.com/man-group/ArcticDB/blob/0c1db33297e16007d501471173e5d36f1ed1845f/python/arcticdb/options.py#L192-L217)
- [A-7] [LICENSE.txt L1–20：BSL 1.1 与“数据库服务”限制](https://github.com/man-group/ArcticDB/blob/0c1db33297e16007d501471173e5d36f1ed1845f/LICENSE.txt#L1-L20)

### OpenBB（提交 `f0f1852b`）

- [O-1] [openbb_platform/core/openbb_core/app/model/obbject.py L33–160、L289–298：OBBject、to_df/to_dataframe、to_polars、to_numpy](https://github.com/openbq-org/OpenBB/blob/f0f1852b96dd3fc4d5868f8863cbe053ff49c6cd/openbb_platform/core/openbb_core/app/model/obbject.py#L33-L160)
- [O-2] [openbb_platform/core/openbb_core/provider/standard_models/equity_historical.py L42–50：OHLC 为 float](https://github.com/openbq-org/OpenBB/blob/f0f1852b96dd3fc4d5868f8863cbe053ff49c6cd/openbb_platform/core/openbb_core/provider/standard_models/equity_historical.py#L42-L50)
- [O-3] [README.md L60–78：openbb-api 在 127.0.0.1:6900 启动 FastAPI](https://github.com/openbq-org/OpenBB/blob/f0f1852b96dd3fc4d5868f8863cbe053ff49c6cd/README.md#L60-L78)
- [O-4] [openbb_platform/README.md L125–140：REST API 包装全部已安装命令](https://github.com/openbq-org/OpenBB/blob/f0f1852b96dd3fc4d5868f8863cbe053ff49c6cd/openbb_platform/README.md#L125-L140)

### 规范与库文档

- [S-1] [PyArrow v26.0.1：pyarrow.decimal128](https://arrow.apache.org/docs/python/generated/pyarrow.decimal128.html)
- [S-2] [Arrow Columnar Format（v26.0.1 文档，格式版本 1.5）：Decimal128 为 16 字节定宽](https://arrow.apache.org/docs/format/Columnar.html)
- [S-3] [Parquet Logical Types：DECIMAL](https://parquet.apache.org/docs/file-format/types/logicaltypes/)
- [S-4] [PyArrow v26.0.1：pyarrow.parquet.write_table（store_decimal_as_integer）](https://arrow.apache.org/docs/python/generated/pyarrow.parquet.write_table.html)
- [S-5] [PyArrow v26.0.1：Pandas Integration（Arrow→pandas 类型表无 decimal 行）](https://arrow.apache.org/docs/python/pandas.html)
- [S-6] [pandas 3.1.1：PyArrow Functionality（ArrowDtype decimal 示例、types_mapper）](https://pandas.pydata.org/docs/user_guide/pyarrow.html)
- [S-7] [Polars 2.1.0：polars.datatypes.Decimal](https://docs.pola.rs/api/python/stable/reference/api/polars.datatypes.Decimal.html)
- [S-8] [DuckDB 2.0：Numeric Types（DECIMAL 宽度、内部存储、宽 decimal 性能、溢出报错）](https://duckdb.org/docs/2.0/sql/data_types/numeric.html)
- [S-9] [Python 3.15.2：decimal 模块（有效位、默认精度 28、normalize、quantize）](https://docs.python.org/3/library/decimal.html)

## 附录 A：实验

目录：`/tmp/talaria-numeric-bench.yYL1U5/`（系统临时目录，重启或系统清理后可能消失）。

| 文件 | 内容 |
|---|---|
| `bench.py` | 生成 100 万行数据，写 5 种单文件表示，测大小、读、转 `Decimal`、转 float；`results.json` 和 `run2.log` 是结果 |
| `bench_per_symbol.py` | 按标的分文件的 `decimal128(18,s)` + `store_decimal_as_integer` 变体；`per_symbol.log` 是结果 |
| `semantics.py` | 边界值、尾零、Parquet 物理类型、pandas/Polars/DuckDB 读回和运算；`semantics.log` 是结果 |
| `bars_*.parquet`、`per_symbol_*.parquet`、`phys.parquet` | 生成的数据文件 |

- A.1 边界值：见 3.6 表。
- A.2 尾零与上下文精度：见 3.6 表。
- A.3 物理类型：`decimal128(38,18)` 与 `decimal128(18,9)`（默认参数）都写成 `FIXED_LEN_BYTE_ARRAY`；`int64` 写成 `INT64`；`decimal128(18,7)` + `store_decimal_as_integer=True` 写成 `INT64` + `Decimal(precision=18, scale=7)`。pyarrow、Polars、DuckDB 读回分别为 `decimal128(18, 7)`、`Decimal(precision=18, scale=7)`、`DECIMAL(18,7)`。
- A.4 库行为：见 3.4 表“实测”行。
- A.5 计时方法：第一次运行没有关 GC，字符串转 `Decimal` 的中位数 1.06 s、最小值 0.49 s，波动很大；正式结果是第二次运行，计时区间内 `gc.disable()`。所有结果都是单机单次运行，只用来比较量级。
- A.6 乘法精度：1000BONK 段前三行 `close*volume`，Python `Decimal`、Polars（`Decimal(38,18)`）和 DuckDB（先 CAST 到 `DECIMAL(38,9)`）结果一致；DuckDB 直接相乘报 `Overflow in multiplication of DECIMAL(38)`。
