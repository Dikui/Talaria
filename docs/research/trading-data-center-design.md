# Talaria 交易数据中心：搭建方案与开源模板选型

> 调研日期：2026-11-16。沿用现有讨论的低频研究、回测、paper-first 假设；市场、频率、预算与数据授权尚未确定。
> 本文是架构建议与一手资料核查，不是已实现能力。未安装这些项目、接入账号、购买数据或运行性能测试；没有修改[现有架构 HTML](<./agentic-trading-architecture.html>)。

## 1. 结论先行

**可以复用开源组件和脚手架，但不应把“行情下载器”直接当作可靠的数据中心。** Talaria 要建立的是一个数据 Module：统一接入、保存来源与修订、校验数据，再向研究和信号模块提供可复现的 `EvidenceSnapshot`。

起步建议：

```text
Python Provider Adapters / 可选 OpenBB Provider
                 ↓
原始响应存档 → 标准化 + 标的/日历映射 → 质量检查/隔离
                 ↓
不可变 Parquet 数据批次 + 原始文档对象
                 ↓
SQLite 元数据、发布记录、快照 manifest
                 ↓
DuckDB 查询固定文件集合 + 时点过滤
                 ↓
Data API → EvidenceSnapshot → Research / Signals / Evaluation
```

- **先单仓库、单机、单发布写入者**；它是现有 Data Module 内部的分层，不是六个微服务。
- **Parquet 存分析数据，DuckDB 查数据，SQLite 管小规模事务元数据**。不是把同一份行情再复制进三个数据库。
- 调度从 CLI + 系统定时任务起步；数据任务依赖、分区回填和多人运维复杂后，再接 Dagster。
- OpenBB 借鉴 Provider 扩展接口；Qlib 借鉴 PIT 因子表示及研究输出；Nautilus Catalog 作为交易回放出口。它们不替代自己的时点、版本、质量和发布契约。
- **开源代码许可 ≠ 行情、新闻和财报内容的使用/再分发许可。** 外部 LLM 调用、展示、缓存和转售都应核查数据源条款。

技术依据：DuckDB 官方支持直接读取 Parquet 文件列表及列/过滤下推；SQLite 官方将本地、低写并发列为合适用途，同时明确每个数据库文件同一时刻只有一个写入者。[DuckDB Parquet][duck-parquet]、[SQLite 适用边界][sqlite]

## 2. 数据中心负责什么，不负责什么

| 负责 | 不负责 |
|---|---|
| 行情、财报、新闻、宏观、交易日历与证券主数据 | 生成交易观点、仓位分配、订单执行 |
| 数据来源、修订版本、时间语义、可用性证据 | 把“最新数据”冒充“历史当时可见数据” |
| 质量状态、缺失原因、授权范围、可复现快照 | 以 Agent 的总结或向量库替代原始事实 |
| 面向研究/信号/回测的一致读取接口 | 资金与成交的权威账本；那属于 Ledger |

收益归因可以读取账本，但数据中心不另建一份能自行改写持仓的账户真相。Embedding、摘要和特征是可重建的衍生数据，必须指回原始对象与生成版本。

## 3. 内部拆成六个部分

| 子模块 | 核心职责 | 第一版交付物 |
|---|---|---|
| ① Provider 接入 | 凭据隔离、限流、分页、超时重试、增量游标、回填；记录请求口径 | 统一 Adapter + 一个市场数据源 |
| ② 标的与日历 | 稳定内部 ID、证券与发行人区分、代码变更、上市/退市、交易时段和公司行动 | 有效期映射、版本化交易日历、公司行动表 |
| ③ 原始存档 | 保存允许缓存的原始响应/文档、来源、抓取批次、内容 hash；避免不可追溯覆盖 | 原始对象 + RawManifest |
| ④ 标准化与修订 | 时间/币种/单位/schema 对齐；同一事实保留多个版本；调整口径明确 | 不可变规范化 Parquet 批次 |
| ⑤ 质量与发布 | 检查范围、唯一性、时序、完整度和新鲜度；隔离坏批次；原子切换可见 manifest | QualityReport + PublishedDatasetVersion |
| ⑥ 快照与查询 | 按知识截止时点选版本，固定文件与策略，返回证据与缺失说明 | EvidenceSnapshot + 只读 Data API |

**不要按供应商分别建一套业务接口。** 供应商是替换边界：各 Adapter 实现统一能力，但必须显式声明是否支持历史修订、退市证券、实时流、复权和特定市场；不能用同名字段掩盖语义差异。

### 建议的最小外部 Interface

以下是拟议接口，不是某个开源项目的现成 API：

```python
create_snapshot(request: SnapshotRequest) -> EvidenceSnapshot
read_bars(snapshot_id: str, query: BarQuery) -> BarResult
read_facts(snapshot_id: str, query: FactQuery) -> FactResult
read_documents(snapshot_id: str, query: DocumentQuery) -> DocumentResult
get_quality(snapshot_id: str) -> QualityReport
```

- `SnapshotRequest` 固定 universe、知识截止时点、事件窗口、数据版本、时点模式、feed、复权口径和质量策略。
- 查询只能缩小该快照的范围，不能悄悄读到更新版本；允许的动态数据读取必须创建新快照。
- 可返回的错误至少区分：`UnauthorizedDataset`、`UnsupportedCapability`、`SnapshotNotFound`、`InsufficientHistory`、`TemporalCoverageUnknown`、`StaleData`、`QualityRejected`。
- 抓取、回填、发布属于写入管理接口；Agent 只有只读查询权，不持有采集密钥、任意文件写权或发布权限。默认不向 Agent 暴露任意 SQL/任意 URL 抓取。

## 4. 最重要的设计：别把几个时间混成一个 timestamp

### 4.1 至少区分这些时间与版本

| 字段/概念 | 意义 | 常见错误 |
|---|---|---|
| `event_time` / `period_end` | 交易发生时刻，或报表覆盖期末 | 把季度结束日当财报公开日 |
| `source_published_at` | 来源声称的发布时刻 | 把首次发布时间套给后来修改的正文 |
| `source_available_at` + 证据等级 | 该内容版本何时经指定渠道可取得；可能只知道日期或保守上界 | 根据记录日期虚构精确可用秒数 |
| `ingested_at` | 本系统何时收到该版本 | 倒填成来源时间，假装系统早就收到了 |
| `committed_at` | 本系统何时完成校验并发布给消费者 | 收到但未校验/未发布的数据提前进入信号 |
| `source_record_id` / `revision_id` / `content_hash` | 事实身份和具体内容版本 | 更新时覆盖旧值，历史实验随之变化 |

这是建议模型；并非每个供应商都提供这些字段。缺少证据时必须标记未知或使用公开记录的保守规则，不能给一个猜测值再宣称严格 PIT。

### 4.2 两种回放模式必须分开

1. **市场历史可知回放（historical PIT）**：模拟当时通过指定数据渠道可能取得的信息。可以今天下载历史版本，但必须有历史版本/可用时间证据，并固定渠道与延迟假设。只有最新修订值的数据源，不能凭一列时间戳恢复旧版本。
2. **系统实际观察回放（observed replay）**：重放 Talaria 当时真正发布过的数据；还要约束 `committed_at <= decision_at`，把延迟、故障与质检等待都算进去。

两者都要固定获取到的数据集版本，且只选择在各自可用规则下已经可见的修订。`snapshot_id` 保证复现，**不自动证明没有前视偏差**。若只有日级可用时间，则不能支持未经额外证据验证的盘中精确回放。

### 4.3 官方接口里已能看到的陷阱

- **财报**：SEC XBRL `frames` 按给定期间选取最匹配的最后提交事实，不是“当时所知”的自动快照；Company Facts 聚合还有非自定义 taxonomy、全实体范围等限制。需要保存 accession、来源报文与具体版本，并连接可用时间证据。[SEC API][sec-api]
- **财报修正/身份**：SEC 说明存在事后修正/删除；CIK 是申报实体 ID，不等于某个证券永久 ID，官方 ticker 映射也不保证完整准确。应单独维护证券主数据。[SEC 数据访问][sec-access]
- **历史 K 线**：Alpaca 的 `asof` 是标的代码映射日期，不是数据修订截止日期；`adjustment` 另有 raw/split/dividend/spin-off 等选项。不能看见 `asof` 就认为接口保证 PIT。[历史行情参数][alp-bars]
- **行情 feed**：IEX 是单交易场所，SIP 是合并 feed；默认选择可能受订阅影响。因此 feed 是数据集身份的一部分，不能自动混用。[行情 FAQ][alp-faq]
- **K 线修订**：分钟 bar 时间戳不等于可用时刻；实时日 bar 会变化，迟到成交可以触发 `updatedBars`，还有成交更正/取消。因此同一根 bar 需要版本和完成状态。[实时行情文档][alp-stream]
- **新闻**：接口有 `created_at` 与 `updated_at`；今天取得的修订正文不能仅按最初发布时间放回历史快照。[新闻接口][alp-news]
- **宏观数据**：FRED 默认实时区间是今天；历史可知值要查 ALFRED/realtime 区间和 vintage。Vintage 日期也不等于精确盘中发布时间。[实时区间][fred-time]、[Vintage dates][fred-vintage]

### 4.4 复权、证券范围与特征的规则

- 同时保存原始价格、公司行动及其版本；复权/总回报序列是明确口径的衍生数据。不能让未来拆股或分红信息无说明地进入过去特征。
- 保存历史 universe 和上市/退市、代码映射变化；用今天的存活证券列表回测历史，仍可能产生幸存者偏差。若只拿得到固定样本，报告必须明确局限。
- 滚动特征只能使用截止时点已可用的记录；标准化器、缺失填充、模型与 embedding 的版本需要固定，拟合范围不能越过训练切分。
- 不把缺失 bar 一律补成零成交，也不一律当作故障。休市、停牌、无合格成交、权限不足、供应商故障、分页未完成需要不同状态。
- `exchange_calendars` 可复用交易时段接口，但官方说明日历由社区维护，默认常规时段、不涵盖个股停牌；需要版本化并用交易所公告校验。[交易日历项目][cal]

## 5. 存储与可靠发布怎么搭

### 5.1 最小可行存储分工

| 组件 | 存什么/做什么 | 不应该做什么 |
|---|---|---|
| 本地文件或对象存储 | 许可允许保存的原始 JSON、文档、压缩响应 | 在日志里记录 token/完整认证请求；无视删除和授权要求 |
| Parquet | 标准化行情、事实、公司行动、文档元数据和衍生表 | 把“文件格式”误认为事务、权限、版本管理系统 |
| DuckDB | 每次按固定 manifest 文件列表做分析查询 | 用变化的目录 glob 冒充已冻结快照；让 Agent 任意写主库 |
| SQLite | 任务、游标、发布版本、manifest、质量结果、授权配置 | 用作海量行情的重复主存；多主机共享文件直接写 |
| 检索索引（后加） | 原文的全文/向量索引，带内容版本 | 成为财务数值、持仓或来源事实的唯一存储 |

DuckDB 可以直接接收显式 Parquet 文件列表；这是冻结读取集合的技术基础，不是内置 PIT 保证。[DuckDB Parquet][duck-parquet]

第一版可按 dataset/market/feed/timeframe/年月组织文件，按实际查询测量再决定粒度，避免每个标的每天产生一个极小文件。原始文档与大型正文不必全塞进行情表。这里未做容量测试，不给虚构的吞吐与成本承诺。

### 5.2 建议的发布协议

```text
取得任务锁 → 按游标抓取并存 raw → 规范化到 staging
       → 校验通过 → 写全新不可变 generation
       → 确认文件与必要目录持久化
       → SQLite 短事务提交 manifest / 发布指针 / 成功游标
       → 新快照可见；旧快照仍引用旧文件
```

这是**需要实现并做崩溃测试的协议建议**，不是 Parquet/DuckDB 自动提供的能力。

- 文件与 SQLite 不存在天然跨介质事务；顺序设计应让崩溃至多留下未被引用的文件，而不是把未完整写好的文件发布出去。
- 本地临时文件到最终路径的原子替换须在支持语义的同一文件系统；durability 还取决于同步、数据库配置与实际存储，不能仅靠 rename 推断断电安全。
- SQLite 数据库、Parquet 文件集、raw 和 manifest 的备份/恢复必须一致；恢复时验证 hash。不能只备份数据库然后假定文件都在。
- 云对象存储不照搬本地 rename：写不可变 key，校验对象存在和完整，再以事务元数据引用；发布指针需并发控制。旧版本回收需检查快照引用和保留政策。
- 同一抓取任务重跑不应重复发布事实；修订是真实新版本，不应被“去重”吞掉。游标只在相应数据成功发布后推进。
- 时间序列最新数据用重叠窗口补抓处理迟到/修订；窗口不是全历史修订保证，必要时做周期性审计或使用源端版本流。
- 快照固定的是 manifest 指向的内容和转换规则，不是一个会自动变动的 `latest/*.parquet` 路径。

### 5.3 什么时候才升级基础设施

- **SQLite → PostgreSQL**：多主机写入、多人权限审计或写入排队已成为实际问题。不要用网络共享 SQLite 文件代替数据库服务。[SQLite 边界][sqlite]
- **本地文件 → 对象存储**：跨机器共享、容量、备份/耐久性需求出现；同时实现新的发布/权限/保留协议。
- **CLI → Dagster**：任务依赖和分区回填需要可视化、重试、数据资产质量追踪时。Dagster 是编排层，不接管金融时点语义。[Dagster Quickstart][dagster]
- **增加流式平台/时序数据库**：只有分钟以下或逐笔吞吐、延迟、持续运行指标需要时，先基准测试，再选择；本次低频假设下不默认引入 Kafka、ClickHouse 或分布式计算。

## 6. 开源模板怎么选

**有模板，但分别解决不同层，不存在本次已核验、可以直接替代上述六个部分的全套成品。** 选型原则是接入模板、调度脚手架、研究数据格式各取所长，不 fork 一个大平台当全部底座。

| 项目 | 核验版本/代码许可 | 可以拿来做什么 | 不能直接指望它做什么 | 建议 |
|---|---|---|---|---|
| **OpenBB Provider Cookiecutter** | Python `openbb-v5.0.0`，Apache-2.0；旧 `v4.7.0` 是 AGPL-3.0 | 生成可安装的 Provider 扩展、查询/返回模型、凭证命名空间、注册与测试骨架 | 示例提取器是固定假数据；不自动提供原始档案、历史修订和快照系统 | **最直接的接入模板**；单一数据源也可只借接口设计 |
| **DuckDB + Parquet** | DuckDB MIT；Parquet 格式规范 Apache-2.0 | 本地查询、清洗、质量检查、列式存储 | 没有自动给外部目录加上历史可用性、全目录事务和授权 | **第一版存储查询核心** |
| **Dagster 官方空项目脚手架** | 当前官方模板；Apache-2.0 | 资产依赖、调度、回填、运行观察 | 不替代数据发布协议和金融 PIT；旧 fully-featured 示例已停止维护 | **任务复杂后再加** |
| **Microsoft Qlib** | `v0.9.8`，MIT | collector 骨架、季度/年度财务 PIT 表示、研究格式导出 | 不能补出供应商缺失的历史版本和真实公告时间 | **研究输出 Adapter / 设计参考** |
| **NautilusTrader Catalog** | 核验 `v1.231.0` 与 2.0 开发快照，分别为 LGPL-3.0-or-later / LGPL-3.0-only 声明 | 类型化行情、交易回放输入 | 不等于通用财报/新闻版本库；开发快照明确拒绝 `as_of` | 选定该引擎后做**派生出口** |
| **AKShare / Tushare** | 核验的软件分别 MIT / BSD-3-Clause；数据另受条款约束 | A 股等市场的数据接入候选 | 不自动授予商业、团队共享和再分发权；不保证全历史 PIT | **先确认用途与授权，再选接口** |
| **FinRL-Meta** | 核验 current master，MIT | 下载→清洗→技术指标→RL 数组的管线参考 | 不等于数据中心；当前一个 AKShare 调用与上游更名存在静态兼容风险 | 只在确定需要 RL 时考虑 |

逐项源码、版本与许可证据见[接入模板核查](<./trading-data-templates-sources.md>)、[存储与编排核查](<./trading-data-storage-sources.md>)。许可证是固定版本的源码声明，不构成对具体部署/再分发义务的法律结论。

### 可直接看的官方入口

1. **OpenBB：** [稳定版 Cookiecutter 文档][obb-template] 和 [Provider 行情模型模板][obb-model]。接口按 `transform_query → aextract_data → transform_data` 分层；生成安装包和测试框架，真实供应商调用仍须实现。该稳定版 [LICENSE][obb-license] 已明确 Apache-2.0，不能沿用旧版许可结论。
2. **Dagster：** [当前 Quickstart][dagster] 和 [空项目模板][dagster-template]；不要从 [UNMAINTAINED 的旧 fully-featured 示例][dagster-old] 开始。本次核查的旧 IO manager 还有覆盖文件、目录 glob 等行为，不是不可变发布的现成实现。
3. **Qlib：** [collector 基类][qlib-collector]、[PIT 文档][qlib-pit] 和 [PIT collector 说明][qlib-pit-collector]。尤其后者明确：缺少公告日期时使用季度 **45 天**、年度 **90 天**日历日偏移；这是估算，不是真实可用时间证据。
4. **Nautilus：** [固定开发版本 Catalog 文档][naut-catalog] 与 [拒绝 `as_of` 的测试][naut-asof]。发行标签 1.231 与 2.0 开发文档的 Python 路径/API/依赖不同，不能交叉复制教程。

官方示例入口命令如下，**只作说明，本轮没有执行**；`uvx` 会下载并运行工具，生成器还可能提示安装依赖，实际采用前须锁定工具与依赖版本，而非持续追 `latest`：

```bash
# OpenBB 文档所示命令，加上已支持的 provider 类型选项
uvx openbb-cookiecutter -e provider

# Dagster 当前官方创建方式；第一版 CLI-only 可暂不使用
uvx create-dagster@latest project talaria-data
```

**数据许可特别提醒：** AKShare 官方声明接口和数据仅用于学术研究、不可商业使用；本次 Tushare 个人数据服务协议限定个人、非商业、不可转让等用途。开源客户端或积分付费不等于数据权利包，团队/商业用途应另行确认适用授权。[AKShare 声明][ak-terms]、[Tushare 协议][ts-terms]

### 6.1 数据源不是代码模板的一部分

| 场景 | 可考虑的来源 | 必须另核查 |
|---|---|---|
| 美股价格 | 券商/持牌行情源；本文核查了 Alpaca 接口 | IEX/SIP、延迟、复权、修订历史、退市范围、展示/再分发权限 |
| 美股财报 | SEC EDGAR 公共 API | accession 与原文、可用时间、taxonomy 覆盖；声明 User-Agent，遵守访问政策 |
| 宏观 | FRED/ALFRED | vintage、日级时间限制、API key 和单系列许可 |
| A 股/港股 | 合法授权的数据服务；AKShare/Tushare 可作为接入候选 | 各接口覆盖、授权、频控、历史修订、长期稳定性 |

SEC 公共查询 API 无需 API key，但访问政策限制总请求速率、要求可识别的 User-Agent，大规模读取宜使用官方 bulk。FRED 则需要 API key；其条款明确第三方系列可能受版权限制，API 可访问不等于可以对外再分发或转交模型服务。[SEC API][sec-api]、[SEC 访问政策][sec-access]、[FRED 条款][fred-terms]

### 6.2 建议的项目骨架（尚未创建）

```text
talaria/data/
  api.py                  # 只读快照/查询 Interface
  contracts.py            # 记录、时间证据、错误与快照契约
  providers/              # 真实替换点：数据源 Adapter
  reference/              # 标的、代码映射、日历、公司行动
  ingestion/              # 限流、分页、游标、回填
  normalization/          # 类型、单位、时间、版本转换
  quality/                # 校验、隔离、质量报告
  storage/                # raw、Parquet、manifest 发布实现
  snapshots/              # 可用版本选择与快照冻结
  cli.py                  # ingest / backfill / validate / publish
  tests/                  # 修订、时点、恢复和 Adapter 契约测试
```

这是逻辑目录草案，不要求每个目录变成独立服务或每一层都有抽象类。先做一条端到端样例，用真实数据源变化来验证 Adapter 边界。

## 7. 先搭哪一版

### 第一阶段：可靠的历史行情切片

范围：一个市场、一个明确 feed、日线、受控 universe、原始价格、必要公司行动与日历、一个 Adapter。不先做新闻全网抓取、多供应商自动切换和向量库。

交付：六个内部部分的最小闭环、固定快照查询、可重跑 CLI、数值策略回测所需数据。数据源没有历史修订/退市覆盖时明确标记，不把该版本称为严格历史 PIT。

### 第二阶段：一条文本/财报数据链路

在价格闭环稳定后，选财报或新闻中的一种：原文→稳定内容 ID→发布/修订证据→标准化→Agent 只读检索。美股可考虑 SEC 财报；其他市场按合法授权来源选接入。不要同时建设十种数据类型。

### 第三阶段：增量更新和 paper 运行

增加调度、重试、数据新鲜度告警、实际观察回放与备份恢复演练。Signals 对关键数据过期默认拒绝生成可交易信号，其他数据的降级策略需显式配置并记录；不得悄悄换数据源或换口径。

### 第一版必须通过的验收

1. 同一 `snapshot_id` 重跑，文件 hash、可见修订和查询结果一致；增加新数据不改变旧快照。
2. 一份报表从初版改为修订版：修订日前的历史可知查询只返回旧版，之后按策略返回修订版；未知时间不能自动通过。
3. 10:01:10 才收到并发布的 10:00 bar，不出现在 10:01:00 的系统实际观察回放里。
4. 分页中断/限流后重试，既不跳页丢记录，也不把重复记录当新事实；游标与发布进度一致。
5. 夏令时、半日市、拆股、代码变更、退市和非交易日有固定测试样本。
6. 写文件后、提交 manifest 前崩溃：该批次不可见；提交后恢复：引用的全部文件存在且 hash 一致。
7. 旧快照不会因合并文件或垃圾回收失效；需要删除/撤权的数据按政策处理并显式标记快照不可再提供。
8. 过期/异常/无权限数据不静默变成“零”或“使用最新替代值”；Agent 不能调用管理写接口或读取密钥。

上述是拟议验收条件，本轮没有执行这些测试。

## 8. 最后需要确认的范围

当前建议不预设你已经选择美股。真正开工前，只需先定四项：

1. **市场**：A 股 / 港股 / 美股 / Crypto，以及是否跨市场。
2. **粒度**：日线 / 分钟 / 逐笔；第一版不同时覆盖所有粒度。
3. **用途和用户**：个人研究、团队内部，还是对外展示/商业服务；是否把内容发送给外部模型。
4. **数据预算与已有权限**：免费源只做原型，还是可购买可用性、修订历史、退市覆盖等保证。

在这些信息未知时，建议先实施“日线价格 + 固定快照 + 质量门禁”的最小数据闭环，而不是先搭一个泛化数据平台。

## 资料

[duck-parquet]: https://duckdb.org/docs/stable/data/parquet/overview
[sqlite]: https://www.sqlite.org/whentouse.html
[dagster]: https://docs.dagster.io/getting-started/quickstart
[sec-api]: https://www.sec.gov/search-filings/edgar-application-programming-interfaces
[sec-access]: https://www.sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data
[alp-bars]: https://docs.alpaca.markets/us/v1.4.2/reference/stockbarsingle-1
[alp-faq]: https://docs.alpaca.markets/docs/market-data-faq
[alp-stream]: https://docs.alpaca.markets/docs/real-time-stock-pricing-data
[alp-news]: https://docs.alpaca.markets/us/reference/news-3
[fred-time]: https://fred.stlouisfed.org/docs/api/fred/realtime_period.html
[fred-vintage]: https://fred.stlouisfed.org/docs/api/fred/series_vintagedates.html
[fred-terms]: https://fred.stlouisfed.org/docs/api/terms_of_use.html
[cal]: https://github.com/gerrymanoim/exchange_calendars
[obb-template]: https://github.com/openbq-org/OpenBB/blob/5a7517ae8bc74ecdcd1a549ed26df8ad131b02d0/cookiecutter/README.md
[obb-model]: https://github.com/openbq-org/OpenBB/blob/5a7517ae8bc74ecdcd1a549ed26df8ad131b02d0/cookiecutter/openbb_cookiecutter/template/%7B%7Bcookiecutter.project_tag%7D%7D/%7B%7Bcookiecutter.package_name%7D%7D/providers/%7B%7Bcookiecutter.provider_name%7D%7D/models/equity_historical.py
[obb-license]: https://github.com/openbq-org/OpenBB/blob/5a7517ae8bc74ecdcd1a549ed26df8ad131b02d0/LICENSE
[dagster-template]: https://github.com/dagster-io/dagster/tree/b0099d257d666c1c95e8079254060d2ab2caa6f3/python_modules/libraries/create-dagster/create_dagster/templates/PROJECT_NAME_PLACEHOLDER
[dagster-old]: https://github.com/dagster-io/dagster/blob/b0099d257d666c1c95e8079254060d2ab2caa6f3/examples/project_fully_featured/README.md
[qlib-collector]: https://github.com/microsoft/qlib/blob/54355232463878d2eebb91fe0ee5fa7fa1f5976c/scripts/data_collector/base.py
[qlib-pit]: https://github.com/microsoft/qlib/blob/54355232463878d2eebb91fe0ee5fa7fa1f5976c/docs/advanced/PIT.rst
[qlib-pit-collector]: https://github.com/microsoft/qlib/blob/54355232463878d2eebb91fe0ee5fa7fa1f5976c/scripts/data_collector/pit/README.md
[naut-catalog]: https://github.com/nautechsystems/nautilus_trader/blob/ea8bc4c08f74d712d1a9cc8a1e27015100e0196f/docs/concepts/data/catalog.md
[naut-asof]: https://github.com/nautechsystems/nautilus_trader/blob/ea8bc4c08f74d712d1a9cc8a1e27015100e0196f/python/tests/persistence/test_parquet_catalog.py#L56-L62
[ak-terms]: https://github.com/akfamily/akshare/blob/25acdfac7ddf3a0721a0ebdcc6a1d7ef3e76abc9/docs/acknowledgements.md
[ts-terms]: https://tushare.pro/document/1?doc_id=405
