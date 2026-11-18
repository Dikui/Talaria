# Talaria 低频交易数据中心：最小开源组合与官方来源核验

核验日期：2026-11-16，Asia/Shanghai。范围：低频研究、回测、paper-first；仅官方文档、官方源码与发行元数据。未安装或运行外部项目、未调用付费数据 API、未登录、未交易。下文“事实”与“建议”分开；脚手架命令是文档核验结果，不是执行成功记录。

## 1. 结论与项目边界

**建议：从 CLI-first 的 `Parquet + DuckDB + SQLite metadata` 开始。** 用一个受控发布者生成不可变数据批次，SQLite 只记录 manifest、血缘、质量检查和发布状态；DuckDB 按 manifest 的明确文件列表分析。需要资产依赖、分区回填、失败重跑和可视化时，再接 Dagster；决定采用 NautilusTrader 回测后，将其 ParquetDataCatalog 作为市场数据的派生读模型，而不是所有研究资料的唯一真相源。这是 Talaria 的组合设计，并非某个项目已有的交易数据中心模板。

**核验结果：在本次四组官方项目/示例中，没有找到可直接开箱作为 Talaria 交易数据中心、同时实现数据授权管理、金融信息可得时间、不可变快照、修订血缘和跨存储原子发布的模板。** 找到的是通用文件分析组件、编排脚手架、ETL 示例和交易引擎的类型化数据目录；尤其 Dagster 的旧 fully-featured 示例已经明确标为 **UNMAINTAINED**。[D1]、[D3]、[N1]

已有项目研究把数据时序与研究记忆、工作流状态、资金订单事实分离；本文保持该边界，见 [系统调研](<./agentic-trading-systems-survey.md>) 与 [执行研究](<./agentic-trading-execution-sources.md>)。不引入 Kafka/Kubernetes；不因“agentic”就把本地批处理变成分布式平台。

### 四组核心的角色（建议）

| 组合 | 最小职责 | 不交给它的职责 |
|---|---|---|
| Parquet + DuckDB | 不可变列式文件；本地 SQL、质量检查、分析与特征构建 | 全目录事务、金融 PIT、跨进程共享可写数据库协调 |
| SQLite，必要时 Postgres | manifest、source/revision、运行血缘、发布指针与约束 | 大量行情列式分析；用普通当前表冒充历史资料库 |
| Dagster | 后续包装现有 CLI/纯函数：资产、分区、重跑、可观察性 | 把 materialization 成功等同于数据发布原子性或策略无前视 |
| NautilusTrader ParquetDataCatalog | 与选定 Nautilus 版本匹配的行情/工具类型及回测输入 | 新闻、财报、所有原始供应商数据的通用数据湖；历史版本数据库 |

## 2. 版本、许可证、依赖：不能混用 `latest` 与稳定发行

下面固定的是**核验依据**，不是建议安装开发分支；正式采用时应锁定实际发行版本及依赖锁文件。

| 对象 | 本次核验版本/依据 | 许可证与关键依赖事实 |
|---|---|---|
| DuckDB | 官方分支快照 `b678afa97b529d5856731e115e31542a72d2d978`；在线 stable 文档 | MIT。Parquet 扩展随大多数客户端分发；可直接扫描/写出 Parquet，无需为了普通本地 Parquet 另搭数据库服务。[P2]、[P3] |
| Apache Parquet | 官方格式说明及 parquet-format LICENSE | 格式规范项目 Apache-2.0；“Parquet”是文件格式，真正读写能力和依赖由 DuckDB、Arrow 等实现提供，不应把格式本身当事务数据库。[P1]、[P4] |
| SQLite / PostgreSQL | 当前官方 WAL、选型、事务隔离与许可证文档 | SQLite public domain；PostgreSQL 使用 PostgreSQL License。SQLite 本地运行，不需要独立数据库服务器；Postgres 是服务器式选项。[M1]、[M4]、[M5]、[M6] |
| Dagster | `b0099d257d666c1c95e8079254060d2ab2caa6f3` | Apache-2.0。当前创建项目文档要求 Python 3.11+；空项目模板约束 `>=3.11,<3.15`。生成器管理 Dagster 依赖，dev group 包括 `dagster-dg-cli`、`dagster-webserver`；Postgres 实例存储另需 `dagster-postgres`。[D1]、[D2]、[D5]、[D6] |
| Nautilus **发行标签** | GitHub latest-release API 返回 `v1.231.0`，发布于 2026-08-02；不把 API 的 latest 误称所有渠道最高版本 | 该标签根配置是 `LGPL-3.0-or-later`，Python `>=3.12,<3.15`；含 pandas、numpy、pyarrow、fsspec、msgspec、portion、click 等，另有编译扩展/构建依赖。[N5]、[N6] |
| Nautilus **开发快照 / latest docs** | `ea8bc4c08f74d712d1a9cc8a1e27015100e0196f`；源码版本 `2.0.0` | [Python 配置](<https://github.com/nautechsystems/nautilus_trader/blob/ea8bc4c08f74d712d1a9cc8a1e27015100e0196f/python/pyproject.toml#L1-L60>)改为 `LGPL-3.0-only`、Python `>=3.13,<3.16`，运行时 `dependencies=[]`，构建采用 maturin；**不表示零原生依赖**：persistence Rust crate 含 Arrow、Parquet、DataFusion、object_store、Tokio 等。[N2]、[N4] |

**建议：Nautilus 接入必须先选发行线，再选择相应文档、路径、API、数据 schema 和许可证；不要把 2.0 latest 示例复制到 1.231 环境。** 如要统一 Python 环境，可先评估 3.13 与锁定发行的 wheel 支持；本次没有安装，未验证平台二进制兼容性。许可证表是源码声明核验，不是对再分发/链接义务的法律结论。

## 3. Parquet + DuckDB：能直接借用的最小能力

### 3.1 已核验事实

官方文档支持显式文件列表、glob、分区目录读取，以及 `COPY` 输出 Parquet；读取具有列裁剪和过滤下推。[P2] 可直接借用其官方 SQL 形式，下面仅将文件名替换成 Talaria 的假设路径：

```sql
-- 文件列表应来自已经提交的 snapshot manifest，而不是扫描可变目录。
SELECT *
FROM read_parquet(['snapshots/s1/part-000.parquet',
                   'snapshots/s1/part-001.parquet']);

COPY (SELECT * FROM staged_rows)
TO 'staging/part-000.parquet'
(FORMAT parquet, COMPRESSION zstd);
```

**嵌入式原生 DuckDB 数据库的并发边界**：一个进程内可以读写；多个进程可只读。单进程多写线程使用 MVCC/乐观并发；追加通常不冲突，同一行并发更新可能冲突并要求重试。不能因此把一个共享可写 `.duckdb` 文件交给多个 Dagster worker。[P3]

**版本纠偏：不要再写“当前 DuckDB 在任何模式都不支持多进程写”。** 本次 stable 文档已讨论 DuckDB 2.0 的 Quack 远程协议，以及 DuckLake + PostgreSQL catalog 的多实例路线；这些不是这里的嵌入式最小方案，也不是直接共享数据库文件的许可。[P3]

### 3.2 限制与建议

- **事实边界**：Parquet 格式文档定义的是列式文件；DuckDB 的文件扫描和数据库事务文档没有把任意外部 Parquet 目录变成跨文件 ACID 数据集。[P1]、[P2]、[P3]
- **推导**：一个 `read_parquet('data/**/*.parquet')` view 会受目录增删/覆盖影响；保留 view SQL 或回测参数不足以重现当时输入。Talaria 应保存精确文件列表、内容 hash、schema 版本、转换代码与依赖版本；旧文件在保留期内不得覆盖。
- **建议**：起步让每个研究进程使用独立的内存/临时 DuckDB 连接，只读已发布 Parquet；共享的 DuckDB 文件若只是派生索引，可重建而非真相源。复杂 SQL、多文件读取的性能应以真实数据测量，不设置虚构的“到 X GB 必须换栈”阈值。

## 4. Metadata：本地 SQLite 优先，Postgres 按并发与运维需求升级

### 4.1 已核验事实

- SQLite 面向本地应用；可以有多个读者，但每个数据库同一时刻只有一个写者。官方建议：写者无法排队、多个客户端经网络直接访问同一个数据库文件时，考虑 client/server 数据库，而非把 SQLite 文件放上共享网络盘。[M1]
- WAL 允许读者与写者并行，但仍是单写者；要求相关进程在同一主机，不适合网络文件系统。长读事务可能阻碍 checkpoint 并令 WAL 增长。[M2]
- WAL 的 `synchronous=FULL` 与 `NORMAL` 有不同掉电耐久性；数据库与 WAL 是关联状态。备份运行中的库应使用官方 backup API 等一致性方法，不能只复制主 `.db` 文件。[M2]、[M3]
- **当前重要安全补丁**：官方 WAL 文档记录 WAL-reset bug，影响 3.7.0–3.51.2，3.51.3 修复，并回补到 3.44.6、3.50.7；涉及多连接写入/checkpoint 的罕见损坏风险。采用前应检查实际 SQLite runtime 版本，不仅看 Python 版本。[M2]
- Postgres MVCC 不等于长期历史查询：默认 Read Committed 每条语句可以看到不同快照；Repeatable Read 稳定的是当前事务内视图；Serializable 仍需处理 `40001` 事务重试。它们不自动保留某条财报在历史决策时刻可得的版本。[M7]、[M8]

### 4.2 Talaria 建议（不是官方 schema）

最小 metadata 表/记录可以是：

- `sources`：数据授权/来源、抓取方式、时间语义说明。
- `source_revisions`：业务键、来源修订号、`event_at`、`published_at`、`available_at`、`ingested_at`、原始响应 hash、被替代版本。
- `snapshots` + `snapshot_files`：不可变 snapshot id、schema id、父快照、每个文件路径/hash/行数、时间范围、质量报告、状态。
- `runs`：参数、代码 commit、依赖锁 hash、输入 snapshot ids、输出 artifact ids。
- `dataset_heads`：每个 dataset 当前已发布 snapshot 指针；与 manifest 在同一个 metadata 事务内更新。

这些是研究/数据运行记录，**不替代独立的订单/成交/对账账本**。控制面 metadata 应小而明确；SQLite 与 Dagster 自身运行库不要混成没有边界的一张“大状态表”。

**升级到 Postgres 的触发条件（建议）**：多个采集/发布进程的写锁等待已违反实测时延目标；需要跨主机访问、服务化权限、统一连接管理/备份恢复；或已有常驻编排服务需要共享 metadata。仅“将来可能规模大”或单纯多读者不是升级理由。升级 metadata 也不自动解决外部 Parquet 文件的事务、历史留存与 PIT。

## 5. Dagster：可借脚手架，不搬旧全家桶

### 5.1 当前官方命令，已核验但未执行

官方 quickstart 与创建项目文档当前使用：[D1]

```bash
uvx create-dagster@latest project my-project
# 生成器会询问是否运行 uv sync；这是安装/解析依赖动作，本次没有执行。
cd my-project
# 已有可用 dg 环境后：
dg scaffold defs dagster.asset assets.py
dg list defs
dg check defs
dg dev
```

若已经安装创建工具，官方另一种写法是 `create-dagster project my-project`。生成树包含 `src/<module>/definitions.py`、`src/<module>/defs/`、tests、项目配置和锁文件。`@latest` 是当前官方示范，不是可重复构建锁；正式采用应锁创建工具/生成依赖版本。[D1]、[D2]

### 5.2 精确官方路径及可借程度

以下路径均在固定 Dagster SHA 核验：

| 路径/证据 | 能借什么 | 不能误认为什么 |
|---|---|---|
| [空项目模板目录](<https://github.com/dagster-io/dagster/tree/b0099d257d666c1c95e8079254060d2ab2caa6f3/python_modules/libraries/create-dagster/create_dagster/templates/PROJECT_NAME_PLACEHOLDER>)；[生成依赖代码](<https://github.com/dagster-io/dagster/blob/b0099d257d666c1c95e8079254060d2ab2caa6f3/python_modules/libraries/create-dagster/create_dagster/scaffold.py#L87-L95>) | 当前模块布局与 Definitions 入口，建议未来从这里开始 | 不包含 Talaria 数据契约、PIT 或发布协议。[D2] |
| [quickstart_etl assets](<https://github.com/dagster-io/dagster/blob/b0099d257d666c1c95e8079254060d2ab2caa6f3/examples/quickstart_etl/src/quickstart_etl/defs/assets.py>)；[依赖](<https://github.com/dagster-io/dagster/blob/b0099d257d666c1c95e8079254060d2ab2caa6f3/examples/quickstart_etl/pyproject.toml>) | HackerNews JSON→CSV→词频图的 assets 思路 | 它含 pandas、matplotlib、boto3、dagster-cloud 等，并非最小交易数据中心。[D4] |
| [fully-featured README](<https://github.com/dagster-io/dagster/blob/b0099d257d666c1c95e8079254060d2ab2caa6f3/examples/project_fully_featured/README.md>) | 仅理解历史设计 | 标题明确 `UNMAINTAINED`；含 dbt/S3/PySpark/Snowflake 等路线，不能当当前推荐 starter。[D3] |
| [Parquet IO manager](<https://github.com/dagster-io/dagster/blob/b0099d257d666c1c95e8079254060d2ab2caa6f3/examples/project_fully_featured/project_fully_featured/resources/parquet_io_manager.py>)；[DuckDB-Parquet IO manager](<https://github.com/dagster-io/dagster/blob/b0099d257d666c1c95e8079254060d2ab2caa6f3/examples/project_fully_featured/project_fully_featured/resources/duckdb_parquet_io_manager.py>) | 分区映射、IOManager 抽象、DuckDB view 思路 | 前者按固定路径写/overwrite，后者先写文件再 `create or replace view ... parquet_scan(...)`；没有跨文件与数据库原子事务，也没有 immutable snapshot manifest。[D3] |

旧例 README 的 `dagster project from-example --name my-dagster-project --example project_fully_featured` 只证明历史示例入口存在；**不把它推荐为当前新项目命令**。旧例还限制 Python `>=3.11,<3.12`，依赖 PySpark、dbt、Snowflake 等；DuckDB IO manager 明确不支持分区子集读取，错误文字为 `Loading a subselection of partitions is not yet supported`。[D3]

### 5.3 编排并发不等于存储并发

**事实**：Dagster OSS 设置 `DAGSTER_HOME` 后可持久化本地实例数据，默认使用 SQLite 存运行/事件历史与本地日志；未设置时临时存储会清理。默认 launcher 为各 run 启动新进程；DefaultRunCoordinator 不提供队列并发限制，QueuedRunCoordinator 和 tag 限流需要相应 daemon 配置。Postgres storage 需额外集成包。[D5]

**建议**：数据发布入口自身保持单写者/锁和幂等键，不把“一个 asset”当锁。接入 Dagster 时把 publication 当有明确成功条件的独立阶段；对同一个 dataset/catalog 的 mutation 串行化。先让 CLI 及其恢复测试可靠，再包 assets；暂不为比较而再引入 Prefect。

## 6. NautilusTrader ParquetDataCatalog：是派生目录，不是 versioned 数据库

### 6.1 官方例子与路径

**当前 latest docs / 开发快照 2.0 路线** 的官方入门形式为：[N1]

```python
from nautilus_trader.persistence import ParquetDataCatalog

catalog = ParquetDataCatalog(str(CATALOG_PATH))
# 后续使用与已锁定版本匹配的 typed write/query API；不在这里执行。
```

可借用的精确依据：

- [官方 catalog 概念文档源码](<https://github.com/nautechsystems/nautilus_trader/blob/ea8bc4c08f74d712d1a9cc8a1e27015100e0196f/docs/concepts/data/catalog.md>)：创建目录、typed writes、DataCatalogConfig / BacktestDataConfig 与迁移示例；当前配置不应混用旧的 flat `catalog_path` 参数。[N1]
- [Rust 写入实现](<https://github.com/nautechsystems/nautilus_trader/blob/ea8bc4c08f74d712d1a9cc8a1e27015100e0196f/crates/persistence/src/backend/parquet/catalog/write.rs#L77-L106>)：按 batch 写入；不是任意整个 dataset 的统一事务。[N3]
- [Python catalog 测试](<https://github.com/nautechsystems/nautilus_trader/blob/ea8bc4c08f74d712d1a9cc8a1e27015100e0196f/python/tests/persistence/test_parquet_catalog.py#L35-L62>)：nanosecond round-trip，以及明确断言 `as_of is not supported by ParquetDataCatalog`。[N3]
- **发行标签 1.231 路线**的不同路径：[Python catalog 实现](<https://github.com/nautechsystems/nautilus_trader/blob/v1.231.0/nautilus_trader/persistence/catalog/parquet.py#L105-L141>)；旧 API 的 `write_data(...)` 和当前 Rust typed writes 不能混为一套。[N6]

### 6.2 并发、覆盖、迁移与再现限制（事实）

- **1.231 发行标签**：源码直说 catalog **not threadsafe**；`write_data` 警告已有同名文件会覆盖，要求同类型数据按 `ts_init` 单调非递减；查询 `start/end` 在 PyArrow 路径按 `ts_init` 过滤。[N6]
- **当前 2.0 文档**：每个 typed write batch 要求相同数据 identity，且 `ts_init` 有序；默认检查区间不重叠，刻意跳过需 `skip_disjoint_check`。单实例 native 文件变化串行化，query/write/catalog 操作不应重叠；这不是多个进程可安全并发修改同一目录的承诺。[N1]
- **当前目录维护不是全目录 ACID**：reset 跨目录并非 all-or-nothing；对象存储 rename 可能是 copy-delete；consolidation 先写合并文件再删源文件，并用 `.consolidation.json` 处理 superseded 文件，分目录完成，先前成功部分不因后续失败回滚。官方要求协调目录 mutation，备份前完成清理；删除是永久操作。[N1]
- 当前 Arrow 时间字段是 `Timestamp(ns, UTC)`；跨 SQL 工具按 TIMESTAMPTZ 读取可能损失纳秒。旧 schema/路径需对应版本的 `nautilus catalog migrate-parquet`；该命令会改变数据，本文只核对文档，未运行。[N1]
- Nautilus 的 `ts_event` 是事件时间，`ts_init` 是对象初始化时间，不保证就是供应商原始发布时间/接收时间；回测按 `ts_init` 组织事件。因此即使成功回放也不能自动证明财报、新闻、修订数据没有前视偏差。[N7]
- 开发快照测试明确拒绝 `as_of`；不能把 catalog 的时间范围查询说成版本时光回溯。[N3]

**建议**：raw/normalized/PIT metadata 自己保留；把某个已提交 snapshot 转换到独立的 Nautilus generation 根目录，转换时锁定精度、工具定义、bar 规则与 schema。发布后不再 consolidation/overwrite 该 generation；将 catalog 根与上游 snapshot id、转换代码、Nautilus 版本绑定。若需维护或迁移，生成新 generation 并验证后发布，不就地破坏旧回测输入。catalog 只在确定采用该引擎后引入。

## 7. 原子发布与快照再现：需要 Talaria 补上的协议

### 7.1 三件不同的事

1. **存储版本/快照**：这次查询读取哪一组字节。
2. **业务 point-in-time**：在决策时间 `T`，哪些信息已经对该策略真实可得。
3. **运行可再现**：相同字节、schema、转换代码、依赖和参数能否再生成相同输入/结果。

上面的 DuckDB/MVCC、Dagster materialization、Nautilus catalog 没有替 Talaria 自动完成这三个条件；各自边界见 [P3]、[M8]、[D3]、[N1]、[N3]、[N7]。**即便未来选用 lakehouse time travel 或 versioned time-series，库写入版本时间也不等于金融信息发布时间过滤。** 本次为控制范围未核查 ArcticDB，不作其能力、许可或并发承诺。

### 7.2 建议的本地发布流程（设计，不是现成模板能力）

1. 单个发布者持有 dataset 级独占写权限；先记录可重试的 run/idempotency key。
2. 在唯一 staging generation 写入新 Parquet，完成 schema、行数、范围、去重、时间语义和质量检查；计算文件 hash。不覆盖历史 generation。
3. 完成文件 flush/fsync，再移动到同文件系统的唯一最终位置；rename **之后**还需按目标 OS/文件系统持久化目标父目录及必要的目录元数据，再进入 metadata 提交。只有 metadata 提交后的文件才对正式读者可见。**成功的同文件系统 rename 可提供原子名称替换，不等于掉电耐久，也不是数据库+文件的共同事务**；Python 官方分别说明 `os.replace` 和 `os.fsync` 的边界。[A1] 目录同步与硬件掉电语义必须在目标环境验证，不能把这份流程当跨平台耐久性证明。
4. 用**一个 SQLite/Postgres 事务**写入完整 manifest、质量结果、血缘，并更新 `dataset_heads` 指针；同时选定并验证 metadata 自身的耐久配置，例如 SQLite WAL 的 `synchronous=FULL`。[M2] 读者先获取已提交 snapshot id 与完整文件列表，再只读这些不可变文件，不使用“当前目录”glob 充当快照。
5. 失败恢复：metadata 提交前崩溃留下的文件为不可见孤儿；提交后文件必须已存在且可校验。需要重启恢复/掉电测试验证顺序，而不是声称两种存储天然 ACID。孤儿回收与历史 GC 均须考虑活跃读取及保留期，不能见到“不是 head”就删。
6. 备份同时包含一致 metadata 与其引用的不可变文件；SQLite 使用一致备份 API。[M3] 定期演练从备份恢复某个旧 snapshot，而非只验证备份文件存在。

### 7.3 PIT 字段与测试（Talaria 建议）

明确区分 `event_at`、供应商 `published_at`、策略环境可用的 `available_at`、本系统 `ingested_at`；保留时间来源、时区、精度及不确定性。供应商历史发布日期不等于本系统当时已经采集。研究应声明采用“历史市场可得”还是“当时本系统实际可得”的重放政策，不能混用。

查询应按业务键在 `available_at <= decision_at` 的修订中选择当时版本，并再检查发布时间/数据源政策；修订后的值不能按原始 `event_at` 回填后直接用于过去决策。信息发布时间缺失时应显式标注不可证明，不补一个看似精确的值。`snapshot_id` 固定输入版本，**不代替**可得时间谓词。

最小验收：

- 同 snapshot id 在重跑/重启后仍返回相同文件与 hash；任何输入覆盖能被发现。
- 人为加入晚到公告、事后更正、拆分调整版本；过去时点只能见到当时符合政策的版本。
- 两个 writer 同时发布：要么排队成功，要么明确冲突，不出现半批文件或 lost update。
- 在写文件、rename、metadata commit 前后注入崩溃，验证“不可见孤儿”与已提交快照可读。
- DuckDB→Parquet→Nautilus 转换检查价格精度、时区、时间戳精度、`ts_init` 排序、重复区间；旧 generation 不受迁移/compaction 影响。
- 从一致备份在新目录恢复，使用锁定代码与依赖重新跑一次小型回测；这才是再现性验收，而非 UI 显示成功。

## 8. 升级次序与明确触发条件（建议）

| 从 → 到 | 触发信号 | 不应作为触发理由 |
|---|---|---|
| 手动 CLI → Dagster | 跨资产依赖、经常性分区回填、失败恢复/运行可视化已成为维护成本 | “用了 agent 就必须 orchestration 全家桶” |
| SQLite → Postgres | 实测写锁排队不满足目标；跨主机/服务权限/集中备份恢复成为需求 | 单纯多读者或未知的未来数据量 |
| 普通 Parquet 分析 → Nautilus catalog | 已选择该回测引擎，需要其类型化行情/回放接口，并已锁定发行线 | 用交易引擎替代通用新闻/财报与原始数据仓库 |
| 本地 immutable files → 对象存储/专用 catalog | 容量、跨机共享或保留成本被测量证明；能重新设计提交协议与 GC | 假定对象存储 rename 与本地 POSIX rename 相同 |
| 单发布者 → 多写者/更强目录层 | 单写者已成为实际瓶颈；需要跨任务事务且可承担协调复杂度 | 把重试、并发跑任务当成自然获得的原子发布 |

本轮没有做性能 benchmark、安装测试或灾难恢复实验，也没有审计供应商数据授权；因此不把示例代码、文档中的保证升级成生产可用认证。

## 官方证据索引

每个编号对应上文事实；固定 SHA 的源码用于避免分支漂移，在线 docs 为本次日期看到的版本。

### Parquet / DuckDB

- [P1] [Apache Parquet 概览](<https://parquet.apache.org/docs/overview/>)。
- [P2] [DuckDB Reading and Writing Parquet Files](<https://duckdb.org/docs/stable/data/parquet/overview.html>)。
- [P3] [DuckDB Concurrency](<https://duckdb.org/docs/stable/connect/concurrency>)；[MIT LICENSE](<https://github.com/duckdb/duckdb/blob/b678afa97b529d5856731e115e31542a72d2d978/LICENSE>)。
- [P4] [Apache parquet-format LICENSE](<https://github.com/apache/parquet-format/blob/master/LICENSE>)。

### Metadata / 原子操作

- [M1] [SQLite Appropriate Uses](<https://www.sqlite.org/whentouse.html>)。
- [M2] [SQLite WAL，包括 §11 WAL-reset bug](<https://www.sqlite.org/wal.html>)。
- [M3] [SQLite Online Backup API](<https://www.sqlite.org/backup.html>)。
- [M4] [SQLite Copyright / public domain](<https://www.sqlite.org/copyright.html>)。
- [M5] [PostgreSQL License](<https://www.postgresql.org/about/licence/>)。
- [M6] [PostgreSQL MVCC Introduction](<https://www.postgresql.org/docs/current/mvcc-intro.html>)。
- [M7] [PostgreSQL MVCC Introduction](<https://www.postgresql.org/docs/current/mvcc-intro.html>)。
- [M8] [PostgreSQL Transaction Isolation](<https://www.postgresql.org/docs/current/transaction-iso.html>)。
- [A1] [Python os.replace](<https://docs.python.org/3/library/os.html#os.replace>)、[os.fsync](<https://docs.python.org/3/library/os.html#os.fsync>)。

### Dagster

- [D1] [Quickstart](<https://docs.dagster.io/getting-started/quickstart>)、[Creating projects](<https://docs.dagster.io/guides/build/projects/creating-projects>)。
- [D2] [项目配置模板](<https://github.com/dagster-io/dagster/blob/b0099d257d666c1c95e8079254060d2ab2caa6f3/python_modules/libraries/create-dagster/create_dagster/templates/PROJECT_NAME_PLACEHOLDER/pyproject.toml.jinja>)、[scaffold.py](<https://github.com/dagster-io/dagster/blob/b0099d257d666c1c95e8079254060d2ab2caa6f3/python_modules/libraries/create-dagster/create_dagster/scaffold.py>)。
- [D3] [旧例 README / UNMAINTAINED](<https://github.com/dagster-io/dagster/blob/b0099d257d666c1c95e8079254060d2ab2caa6f3/examples/project_fully_featured/README.md>)、[依赖](<https://github.com/dagster-io/dagster/blob/b0099d257d666c1c95e8079254060d2ab2caa6f3/examples/project_fully_featured/pyproject.toml>)、[Parquet IO manager](<https://github.com/dagster-io/dagster/blob/b0099d257d666c1c95e8079254060d2ab2caa6f3/examples/project_fully_featured/project_fully_featured/resources/parquet_io_manager.py>)、[DuckDB IO manager](<https://github.com/dagster-io/dagster/blob/b0099d257d666c1c95e8079254060d2ab2caa6f3/examples/project_fully_featured/project_fully_featured/resources/duckdb_parquet_io_manager.py>)。
- [D4] [quickstart_etl assets](<https://github.com/dagster-io/dagster/blob/b0099d257d666c1c95e8079254060d2ab2caa6f3/examples/quickstart_etl/src/quickstart_etl/defs/assets.py>)、[quickstart_etl 配置](<https://github.com/dagster-io/dagster/blob/b0099d257d666c1c95e8079254060d2ab2caa6f3/examples/quickstart_etl/pyproject.toml>)。
- [D5] [Dagster OSS instance configuration](<https://docs.dagster.io/deployment/oss/oss-instance-configuration>)。
- [D6] [Dagster Apache-2.0 LICENSE](<https://github.com/dagster-io/dagster/blob/b0099d257d666c1c95e8079254060d2ab2caa6f3/LICENSE>)。

### NautilusTrader

- [N1] [当前 catalog 文档](<https://nautilustrader.io/docs/latest/concepts/data/catalog/>)；[固定源码版本](<https://github.com/nautechsystems/nautilus_trader/blob/ea8bc4c08f74d712d1a9cc8a1e27015100e0196f/docs/concepts/data/catalog.md>)。
- [N2] [开发快照 Python 配置与许可证声明](<https://github.com/nautechsystems/nautilus_trader/blob/ea8bc4c08f74d712d1a9cc8a1e27015100e0196f/python/pyproject.toml#L1-L60>)。
- [N3] [catalog 测试，含拒绝 as_of](<https://github.com/nautechsystems/nautilus_trader/blob/ea8bc4c08f74d712d1a9cc8a1e27015100e0196f/python/tests/persistence/test_parquet_catalog.py#L35-L62>)；[Rust 写入实现](<https://github.com/nautechsystems/nautilus_trader/blob/ea8bc4c08f74d712d1a9cc8a1e27015100e0196f/crates/persistence/src/backend/parquet/catalog/write.rs#L77-L106>)。
- [N4] [persistence Cargo.toml 原生依赖](<https://github.com/nautechsystems/nautilus_trader/blob/ea8bc4c08f74d712d1a9cc8a1e27015100e0196f/crates/persistence/Cargo.toml#L58-L90>)。
- [N5] [官方 GitHub latest-release API](<https://api.github.com/repos/nautechsystems/nautilus_trader/releases/latest>)；[v1.231.0 release](<https://github.com/nautechsystems/nautilus_trader/releases/tag/v1.231.0>)。
- [N6] [v1.231.0 依赖/许可证](<https://github.com/nautechsystems/nautilus_trader/blob/v1.231.0/pyproject.toml#L1-L52>)；[不线程安全](<https://github.com/nautechsystems/nautilus_trader/blob/v1.231.0/nautilus_trader/persistence/catalog/parquet.py#L105-L141>)；[write_data 覆盖/排序](<https://github.com/nautechsystems/nautilus_trader/blob/v1.231.0/nautilus_trader/persistence/catalog/parquet.py#L255-L306>)；[ts_init 过滤](<https://github.com/nautechsystems/nautilus_trader/blob/v1.231.0/nautilus_trader/persistence/catalog/parquet.py#L2118-L2166>)。
- [N7] [Nautilus Data 概念与时间语义](<https://nautilustrader.io/docs/latest/concepts/data/>)。
