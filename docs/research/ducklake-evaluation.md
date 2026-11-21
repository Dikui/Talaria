# DuckLake 评估：能否替代自写的 Parquet generation + SQLite 清单

- 调研日期：2026-11-20。
- 范围：对应 [platform-redesign 需求](<../../.scratch/platform-redesign/spec.md>) 的 D1（常驻服务是行情历史的唯一写入方）、D2（策略进程、回测、Notebook 在服务之外直读历史）和尚未决定的 D3（取数接口）。问题是：DuckLake 能否替代 [`cold_store.py`](<../../core/src/talaria/perception/cold_store.py>) 和 [ADR-0011](<../adr/0011-cold-store-publish-on-snapshot-freeze.md>) 的自写方案（不可变 Parquet generation、`data_manifest.sqlite`、staging 质检 → fsync → rename → 清单短事务）。
- 方法：
  - 浅克隆官方仓库，按固定提交阅读文档源码和扩展源码。官网 ducklake.select 由 `duckdb/ducklake-web` 生成，文中的 URL 是该仓库文件对应的官网地址。
  - 用 `gh` 查看官方仓库的 issue 和 PR。
  - 在系统临时目录做了一次本机实验。实验没有往项目 `.venv` 安装任何包，而是用 `uv run --no-project --python .venv/bin/python --with duckdb==2.0.1 --with pyarrow` 建了一个一次性环境。
- 固定版本：
  - 文档：`duckdb/ducklake-web` [`f0ccdb7b`](https://github.com/duckdb/ducklake-web/tree/f0ccdb7b651836c75d8608d9f11eacea8b787482)（2026-11-20）。
  - 扩展源码 `main`：`duckdb/ducklake` [`9c5533b0`](https://github.com/duckdb/ducklake/tree/9c5533b096190d9cd5cf11f53c47b76f4002d4af)（2026-11-20）。
  - 实测用的扩展构建：DuckDB 2.0.1 自动安装的 `ducklake` 扩展，版本 `08089b16dc`，即 `v2.0-cyanoptera` 分支提交 [`08089b16`](https://github.com/duckdb/ducklake/commit/08089b16dc8a18ce04abb6efd87e25cb4d64803a)（2026-11-13）。`sqlite_scanner` 扩展版本 `ce0987fda4`。
- 标注约定：
  - **事实**：官方文档、规范或源码直接写明的内容。
  - **厂商宣称**：官方的定性说法或官方自测数字，没有独立复现。
  - **实测**：本文附录实验的观察，只代表本机、本版本、短时间运行。
  - **推断**：面向 Talaria 的判断，不是 DuckLake 的功能。

不重复的已有结论：

- DuckDB 原生库文件的并发规则（一个进程读写，或多个进程 `READ_ONLY`）以及 DuckLake + PostgreSQL 多实例路线的版本纠偏，见 [trading-data-storage-sources.md §3](<./trading-data-storage-sources.md>) 和 [unified-strategy-runtime-sources.md §1 第 5 条](<./unified-strategy-runtime-sources.md>)。
- 发布协议和多种时间戳为什么要分开，见 [trading-data-center-design.md §4–5](<./trading-data-center-design.md>)。本文只回答 DuckLake 能否承担其中的“清单 + 文件 + 压实”部分。

## 1. 结论先行

1. **DuckLake 正式标为生产可用。**规范 1.0 于 2026-04-13 发布（厂商宣称“生产就绪、保证向后兼容”）。当前规范是 1.1，与 DuckDB 2.0（2026-10-21）同时发布；今天（2026-11-20）DuckDB 2.0.1 发布。许可证为 MIT。参考实现只有 DuckDB 的 `ducklake` 扩展。DuckDB 之外，官方博客只介绍过两个实现：一个是官方开发者用 AI 生成的 Python 库 `ducklake-dataframe`，博文自称“别太当真”；另一个是 DataFusion 实现。pyarrow 不能绕开 catalog 直接读出正确的表。[K15]、[B1]、[B2]、[B4]、[B5]、[G1]
2. **SQLite catalog 下“一个写进程 + 多个读进程”是官方推荐的本机多客户端用法，本次实测没有出错。**官方的原话是：SQLite 适合 *local data warehousing using multiple local clients*，DuckLake 会重试 SQLite 的锁冲突。[K1] 实测条件：一个写进程每秒 4 次提交，每次 5 行 `DECIMAL(38,18)`；两个读进程每 50 ms 读一次；运行 60 秒；期间做了 inline flush 和文件合并。结果是 0 个锁错误、0 次不一致快照，读者始终看到完整的批次和精确的 Decimal 和。两个写进程同时写也是 0 个错误。DuckLake 有快照隔离，读者在一个事务里看到的是同一快照。[K3] 但 SQLite catalog 的锁问题在 2026 年 6–10 月连续修过三次（#128、#1309、#1798），这是需要正视的风险信号。
3. **小批量频繁追加有官方方案，而且不需要停写。**每次小于 `data_inlining_row_limit`（默认 10 行）的写入不生成 Parquet，直接写进 catalog 的 inlined 表。之后用 `ducklake_flush_inlined_data` 落成 Parquet，用 `ducklake_merge_adjacent_files` 合并小文件。合并后的文件内嵌每行的快照号，时间旅行不受影响。旧文件要等 `expire_snapshots` 和 `cleanup_old_files` 才会删除。[K5]、[K6]、[K7]、[K8] 实测中发现一个坑：在 Python 里用 `SELECT … FROM ducklake_flush_inlined_data(...)` 再调用 `fetchone()`，flush 会**静默不提交**，返回值显示已刷出行数，但文件被删掉、没有生成快照，也没有报错。改用 `fetchall()` 或 `CALL` 就正常（附录 A.6）。官方仓库里没有查到对应的 issue。
4. **DuckLake 的时间旅行是“提交时间（库里当时有什么）”，不是“信息可得时间”，两者可以互补，但不能互相替代。**`AT (VERSION => n)` 和 `AT (TIMESTAMP => t)` 还原的是某次提交后的表状态，语义接近我们的 `ts_committed`。`ts_available <= as_of` 仍然要靠行级过滤。在只追加、带 revision 行的表上，`ts_committed <= T` 的行级过滤本身就能还原“当时看到的版本”，所以快照时间旅行的增量价值在于原地更新、删除和 schema 变更。一旦用 `expire_snapshots` 过期快照，就无法再回到那个时点（实测报错 `No snapshot found`）。[K10]、[K7]（推断＋实测）
5. **决定性的取舍在读路径和版本耦合。**采用 DuckLake 后，所有读者都必须经过 DuckDB 加 `ducklake` 和 `sqlite` 扩展，或另一个规范实现：小批量数据在 SQLite 里，不在 Parquet 里；合并文件里混有多个快照的行和内部列；被替换的旧文件在清理前仍留在目录中。实测用 pyarrow 直接 glob 数据目录，读出 1984 行，真实表只有 1175 行。此外，不同 DuckDB 版本的客户端混用同一个 catalog 已出过事故（#1860 已修；#1924 仍 open）。自写清单的优势是 pyarrow 直读、无版本耦合、质检和内容寻址可控；代价是压实、时间旅行、文件剪枝、崩溃后孤儿清理都要自己写。§8 给出逐项比较。

## 2. DuckLake 是什么、什么版本、多成熟

| 项 | 内容 | 类别 | 来源 |
|---|---|---|---|
| 组成 | 一个支持事务和主键的 SQL 数据库作为 catalog，加上 Parquet 数据文件。catalog 可以用 DuckDB、SQLite、PostgreSQL、MySQL（不推荐），也可以用 Quack 协议托管的 DuckDB | 事实 | [K15]、[K1] |
| 规范版本 | 0.1（2025-05-27）→ 0.2 → 0.3 → 0.4（2026-03-09）→ 1.0（2026-04-13）→ 1.1（2026-10-21，随 DuckDB 2.0 发布）。“规范版本目前与扩展版本绑定发布” | 事实 | [K23]、[B2] |
| 扩展与 DuckDB 的对应 | 1.0 扩展对应 DuckDB 1.5.2–1.5.6；DuckDB 2.0 带新扩展（规范 1.1），能读写 1.0 catalog。1.0 catalog 要显式加 `AUTOMATIC_MIGRATION` 才会升级到 1.1，升级后不可降级 | 事实 | [K20]、[B2] |
| 实测版本 | DuckDB 2.0.1（GitHub release，2026-11-20）；`ducklake` `08089b16dc`；新建 catalog 的 `version` 选项为 `1.1` | 实测 | [D1]、附录 A.2 |
| 许可证 | 规范和扩展都是 MIT | 事实 | [G1]、[K21] |
| 成熟度声明 | 1.0 博文标题是 “Reaches Production-Readiness”，正文写 “production-ready release with guaranteed backward-compatibility”；FAQ 也写明“生产可用”。只有删除向量（deletion vectors，Puffin）明确标为 **Experimental** | 厂商宣称／事实 | [B1]、[K21]、[K4] |
| 治理 | 由 DuckDB Foundation 管理；路线图上 2.0 的重点是分支和克隆、受保护快照、REST API、权限，且注明“不作保证” | 事实 | [K22] |
| DuckDB 之外的实现 | ① `ducklake-dataframe`：官方开发者用 AI 写的纯 Python 库，支持 Pandas、Polars、PySpark，依赖 PyArrow，catalog 可以是 SQLite、Postgres 或 DuckDB。博文自称与 1.0 读写对等，又自嘲“completely bug-free”，并称其 benchmark “Don't take these benchmarks too seriously”。② DataFusion 实现：第三方公司贡献给 datafusion-contrib，自称已在生产使用。FAQ 欢迎其他系统按规范实现 | 厂商宣称 | [B4]、[B5]、[K21] |
| pyarrow 直读 | 规范没有提供“只读 Parquet 就能得到表”的路径：读一张表要先查 catalog 拿到文件列表，再合并 delete file，按 `partial_max` 和 `_ducklake_internal_snapshot_id` 过滤，最后还要并上 inlined 表 | 事实 | [K17] |

## 3. 并发语义（重点）

### 3.1 官方怎么说

| catalog | 官方描述 | 来源 |
|---|---|---|
| DuckDB 文件 | “single client”；只有当所有进程都以 `READ_ONLY` 打开时才能多进程，同一进程内可以多个连接 | [K1] |
| **SQLite** | “local data warehousing using **multiple local clients**”。“SQLite allows only one writer at a time. DuckLake retries lock conflicts when using a SQLite catalog. This allows a reasonable amount of multi-processing support (effectively hiding the single-writer model).” 必须用 `sqlite:` 前缀或 `META_TYPE 'sqlite'` | [K1] |
| PostgreSQL | 多用户 lakehouse，可以有远程客户端 | [K1] |
| Quack | 用一个 Quack 服务托管 DuckDB catalog，供多个客户端连接 | [K1] |

- **快照隔离（事实）**：DuckLake “has full support for ACID and offers snapshot isolation”；一次 `BEGIN…COMMIT` 对应一个快照；只读事务不生成快照。[K3]
- **冲突检测（事实）**：快照号连续递增，`ducklake_snapshot.snapshot_id` 是主键。两个写者抢同一个快照号时，一方会违反主键约束；这时扩展去查 `ducklake_snapshot_changes`，如果双方没有逻辑冲突（例如两边都只是插入同一张表），就只在 catalog 里重试，不重写数据文件。有逻辑冲突（例如一方删除、另一方压实同一个文件）则中止。[K2]
- **SQLite 锁重试（事实）**：元数据写入和冲突检查阶段的 `database is locked` 会按重试参数重试，**但 SQLite 最终 `COMMIT` 阶段的锁错误不重试**。[K2] 默认参数：`ducklake_max_retry_count=10`、`ducklake_retry_wait_ms=100`、`ducklake_retry_backoff=1.5`。[K4] 源码里 SQLite 的 `busy_timeout` 默认 5000 ms。[G2] 按错误消息匹配是否可重试的逻辑见 [G3]。
- **时点附着只能只读（事实）**：`ATTACH … (SNAPSHOT_VERSION n)` 或 `SNAPSHOT_TIME` 会强制只读模式。[G4]、[K10]

### 3.2 官方仓库里的 SQLite 锁问题（事实）

| issue | 现象 | 状态 |
|---|---|---|
| [#128](https://github.com/duckdb/ducklake/issues/128) | 三个并发的大插入在 SQLite catalog 上全部以 `database is locked` 失败 | 已关闭（2026-03-24） |
| [#1309](https://github.com/duckdb/ducklake/issues/1309) | 读锁升级为写锁时，SQLite 可能不经过 busy handler 直接返回 BUSY；`COMMIT` 不重试 | 已关闭（2026-09-04，PR [#1420](https://github.com/duckdb/ducklake/pull/1420)） |
| [#1798](https://github.com/duckdb/ducklake/issues/1798) | 同一 DuckDB 实例里两个重叠提交都失败后，遗留 hot journal 和写锁，**其他进程也读不了**，直到 DETACH 或重启。报告者在 DuckDB 2.0 dev 版上仍然复现 | 已关闭（2026-10-20，维护者称由 #1680、#1806 修复；另一个相关修复是 duckdb-sqlite#256） |

推断：这三个问题都由“同一 catalog 上有多个写事务重叠”触发。Talaria 的 D1 规定服务是唯一写者，压实也由服务自己串行执行，正好避开最容易出事的路径。读者只读，不会和写者争写锁。即便如此，#1798 的后果是“整个湖不可读”，如果采用 DuckLake，应把“写者进程内只用一个连接、串行提交”写成约束。

### 3.3 实测（附录 A.3、A.7）

环境：Apple M 系列 Mac、Python 3.15.2、DuckDB 2.0.1、`ducklake` `08089b16dc`。SQLite catalog 文件的 `journal_mode` 实测为 `delete`，DuckLake 没有把它改成 WAL。

| 场景 | 写者 | 读者 | 锁错误 | 不一致快照 |
|---|---|---|---|---|
| A：inlining 开（默认 10 行），每 20 s flush+merge | 236 次提交，p50 10 ms，p99 52 ms | 常驻连接：827 次读，p50 9.9 ms。每次重新 ATTACH：461 次，p50 64 ms | 0 | 0 |
| B：inlining 关（每次提交写 Parquet），每 20 s merge | 235 次提交，p50 24.6 ms，p99 91 ms | 常驻连接：612 次，p50 33 ms。重新 ATTACH：378 次，p50 88 ms | 0 | 0 |
| 两个写进程同时写（inlining 关），30 s | 各 116 次提交，p50 33 ms 和 105 ms | — | 0 | 合计 1160 行，116 个批次 × 2 个写者，无丢失 |

- “一致”的判定：读者在一个事务里检查四件事。每个 `batch_id` 恰好 5 行；`count(*) = 批次数 × 5`；批次号连续；`sum(close)` 与按批次号算出的精确 Decimal 和完全相等。此外，相邻两次读看到的最大批次号不能回退。
- 推断：每次重新 ATTACH 要多花约 55 ms。这对 Notebook 和回测启动可以忽略，但策略进程应保持连接常驻。

## 4. 写入模式：小批量频繁追加、压实与清理

| 机制 | 要点 | 类别 | 来源 |
|---|---|---|---|
| 不开 inlining | 每次插入至少写一个新 Parquet。官方建议文件至少几 MB，小文件要合并 | 事实 | [K6]、[K9] |
| Data inlining | 插入行数不超过 `data_inlining_row_limit`（默认 10）时，行写进 catalog 的 `ducklake_inlined_data_<table>_<schema_version>` 表，带 `_ducklake_begin_snapshot` 和 `_ducklake_end_snapshot`。查询时与 Parquet 透明合并。行数上限可以全局、按 ATTACH 或按表持久设置。SQLite catalog 把 `DECIMAL` 存成 TEXT，读回时再转换 | 事实 | [K5]、[K16] |
| Flush | `ducklake_flush_inlined_data` 把 inlined 行落成 Parquet。表有排序键时按排序键写出，分区表按分区写出。flush **不会自动发生**，`auto_compact` 只决定表是否参与维护调用 | 事实 | [K5]、[K13] |
| 合并 | `ducklake_merge_adjacent_files` 把小文件合并成 “partial data file”：每行的快照号写进内部列 `_ducklake_internal_snapshot_id`，`partial_max` 记录上界，所以**不必先过期快照**，时间旅行和变更流也不受影响。可以按大小、`newer_than`/`older_than` 和 `max_compacted_files` 分批，官方给出了分层压实的示例。只有分区规格和分区值相同的文件才会合并 | 事实 | [K6] |
| 过期与清理 | 正常运行时**从不删除数据**。`ducklake_expire_snapshots` 过期快照；被合并或过期掉的文件先进入 `ducklake_files_scheduled_for_deletion`，再由 `ducklake_cleanup_old_files` 删除，因为可能还有查询在读它们。`ducklake_delete_orphaned_files` 清理 catalog 不认识的文件。`CHECKPOINT` 一次跑完上述全部步骤 | 事实 | [K7]、[K8]、[K9] |
| 官方性能数字 | 流式基准（每秒 100 行，分 10 批，PostgreSQL catalog + S3）：开 inlining 后插入快 5.2 倍、聚合快 926 倍。1.1 相对 1.0：提交 10 万行 inlined 数据从 4.6 s 降到 0.10 s（DuckDB catalog）。SQLite catalog 没有给出数字 | 厂商宣称 | [B3]、[B2] |

实测（附录 A.3）：

- 场景 A 跑 60 秒共 1180 行，两次 flush 分别写出 400 行和 395 行，生成 27 个 Parquet 文件（每个月分区一个），第二次 merge 把 2 个文件合成 1 个。场景 B 跑 60 秒生成约 1200 个小文件，每次 merge 把约 400 个文件合成 14 个，耗时约 120 ms。
- **坑（实测，附录 A.6）**：`con.execute("SELECT sum(rows_flushed) FROM ducklake_flush_inlined_data('lake')").fetchone()` 之后再执行下一条语句，flush 事务会被静默回滚：返回值是 3，但 catalog 里 0 个文件，磁盘上的文件也被删掉，没有快照，没有异常。`fetchall()` 和 `CALL …` 都正常。原生 DuckDB 表上的 `INSERT … RETURNING` 加 `fetchone()` 正常提交，说明这个现象和 DuckLake 有关。根因未查明，官方仓库没有查到对应 issue。

## 5. 类型、分区、统计信息

| 项 | 内容 | 类别 | 来源 |
|---|---|---|---|
| `DECIMAL(P,S)` | 规范支持定点小数。列统计以十进制字符串存放，与精度、标度无关 | 事实 | [K16] |
| 读回类型 | Parquet 里是 `decimal128(38, 18)`，带 `PARQUET:field_id`；DuckDB Python 读回 `decimal.Decimal`。`100.000000000000000001` 和 `1E-18` 的倍数都精确往返 | 实测 | 附录 A.3、A.5 |
| 已知类型问题 | SQLite catalog 上 inlined 的 VARIANT 里的 DECIMAL 会丢失声明宽度（[#1942](https://github.com/duckdb/ducklake/issues/1942)，open）；HUGEINT/UHUGEINT 曾被存成 DOUBLE（[#1186](https://github.com/duckdb/ducklake/issues/1186) open，[#1867](https://github.com/duckdb/ducklake/issues/1867) 已关闭）。普通 `DECIMAL` 列不受这两个问题影响 | 事实 | issue |
| 约束 | 不支持主键、唯一约束、外键，未来也不太可能支持；upsert 只能用 `MERGE INTO` | 事实 | [K14] |
| 分区 | 支持 `identity`、`bucket`、`year/month/day/hour`（日历值，例如 1–12 月）和 1.1 新增的 `epoch_year/month/day/hour`（从 1970 起累计）。分区键只影响新写入的数据；默认使用 Hive 风格目录 | 事实 | [K12] |
| 分区与毫秒 int64 | 时间类变换作用在 timestamp 上。实测给表加一个派生列 `ts_event_t TIMESTAMP` 后，`PARTITIONED BY (epoch_month(ts_event_t))` 写出 `epoch_month=674/…` 目录。用 `identity` 分区一个自存的月份整数列也可以 | 实测／推断 | 附录 A.2 |
| 排序 | `SORTED BY (...)` 在 INSERT、flush 和合并时生效，目的是收紧 min/max | 事实 | [K13] |
| 剪枝 | 查询规划时用 `ducklake_file_column_stats` 的每文件 min/max 跳过文件。1.1 起覆盖跨列 `OR`、`IS NULL`、STRUCT 字段和 hash join build 侧下推 | 事实 | [K12]、[K17]、[B2] |
| 剪枝实测 | 场景 B 有 402 个存活文件：`ts_event BETWEEN …`（一个月）读 1 个文件，`ts_available <= …` 读 2 个文件 | 实测 | 附录 A.5 |

## 6. 时间旅行与“还原当时看到的版本”

- **语法（事实）**：`SELECT … FROM tbl AT (VERSION => 3)`；`AT (TIMESTAMP => now() - INTERVAL '1 week')`；`ATTACH … (SNAPSHOT_VERSION 3)` 或 `(SNAPSHOT_TIME '…')`。不接受未来时间戳；事务内不能看到事务开始之后提交的快照。[K10]、[K3]
- **语义（事实）**：每个快照是一次提交后的数据库状态，`snapshot_time` 是提交时间。[K11] 过期快照后就无法再回到那个时点。[K7]
- **实测（附录 A.4）**：同一根 K 线先写 revision 0（`ts_available=1060`），再写 revision 1（`ts_available=5000`）。结果如下：
  - 行级 `ts_available <= 2000` 取最新 revision，得到 rev 0；
  - `AT (VERSION => rev0 的快照)` 得到 rev 0；
  - 用介于两次提交之间的 `AT (TIMESTAMP)` 得到 rev 0；
  - flush 之后再按快照查，仍得到 rev 0；
  - 过期该快照后再按快照查，报错 `No snapshot found at version 2`；
  - 查询未来时间戳，报错 `Cannot time travel to a future timestamp`。
- **与行级 `ts_available` 截断的关系（推断）**：
  - 快照时间约等于我们的 `ts_committed`，即本库何时知道这件事；`ts_available` 是市场上何时能知道。两者不能互相替代：回测防前视看 `ts_available`，复现“某次实盘决策时库里有什么”看快照或 `ts_committed`。
  - 如果表只追加、修订一律写成新的 revision 行，那么 `ts_committed <= T` 的行级过滤已经能还原“当时看到的版本”，快照时间旅行是第二种实现，可用来交叉校验。只有发生原地 UPDATE、DELETE 或 schema 变更时，快照才不可替代。
  - 依赖快照就意味着不能随便 `expire_snapshots`，存储会随修订单调增长。这和“只追加”的取向一致，但要预算磁盘。

## 7. 故障语义与直读

- **写入顺序（事实＋实测）**：先写 Parquet，再在 catalog 里一次性提交快照。规范把孤儿文件列为“例如系统故障”时可能出现的情况，并提供 `ducklake_delete_orphaned_files` 来清理。[K8] 实测（附录 A.7）：一个大插入写到第 2 个文件时被 `kill -9`。之后 catalog 只有崩溃前已提交的 1 行；磁盘上留下 1 个约 290 MB 的孤儿文件；dry run 能识别出它，清理后写入正常。
- **catalog 与文件的一致性（事实）**：DuckLake 只读 catalog 列出的文件。被合并替换的文件和被过期的文件仍留在磁盘上，直到执行清理。[K6]、[K8] 备份时要暂停写入、压实和清理，把 catalog 和它引用的文件一起备份；catalog 备份点之后提交的数据在恢复后不可见。[K19]
- **能否不经 DuckDB 直读 Parquet（事实＋实测）**：
  - 单个文件是标准 Parquet，pyarrow 能读，列类型正确。但合并文件会多出 `_ducklake_internal_row_id` 和 `_ducklake_internal_snapshot_id` 两列，而且混有多个快照的行。
  - inlined 的近期数据根本不在 Parquet 里。
  - 实测场景 B：pyarrow 直接 glob 数据目录读到 1203 个文件、1984 行，表的真实行数是 1175（402 个存活文件）；场景 A 读到 823 行，真实行数是 1180（其中 385 行还在 SQLite 里）。
  - 要正确读，必须按规范的查询逻辑走 catalog。[K17]
- **数据路径写死（实测）**：catalog 里记录的是绝对 `DATA_PATH`。复制目录后用新路径 ATTACH 会报错，要加 `OVERRIDE_DATA_PATH`。推断：迁移数据目录时需要额外处理。

## 8. 风险：格式稳定与版本兼容

| 风险 | 内容 | 类别 | 来源 |
|---|---|---|---|
| 规范兼容承诺 | 1.0 起“保证向后兼容”；新扩展能读写 1.0 catalog，升级到 1.1 需要显式迁移，而且不可降级 | 厂商宣称／事实 | [B1]、[K20] |
| 旧客户端读新 catalog | 1.0 扩展（DuckDB 1.5.x）读不了 1.1 catalog（[#1825](https://github.com/duckdb/ducklake/issues/1825)）；DuckDB 2.0 往 1.0 catalog 写入 `timestamptz_ns` 列后，所有 1.5 客户端都无法读取任何表（[#1860](https://github.com/duckdb/ducklake/issues/1860)，已修）；DuckDB 2.0 的 DELETE 清掉列统计，导致 1.5 客户端崩溃（[#1924](https://github.com/duckdb/ducklake/issues/1924)，**open**，在 2.0.1 `08089b16dc` 上仍能复现） | 事实 | issue |
| DuckDB 文件做 catalog | 新版 DuckDB 的写入可能让旧版 DuckDB 读不了 catalog 文件，官方建议所有客户端用同一版 DuckDB，或改用 PostgreSQL/SQLite | 事实 | [K20] |
| 实验特性 | 删除向量（Puffin）是 Experimental | 事实 | [K4] |
| 维护量 | 截至 2026-11-20，仓库共 67 个 open issue；1.1 博文称自 1.0 起合并了“200 多个 bug fix” | 事实／厂商宣称 | GitHub API、[B2] |
| 扩展分发 | `INSTALL ducklake` 和 `INSTALL sqlite` 首次运行要从官方扩展仓库下载（实测 `install_mode = REPOSITORY`）。离线机器要预装 | 实测 | 附录 A.1 |

## 9. 对 Talaria 的启示：继续自写清单 vs 采用 DuckLake

以下是比较，不是决定。

| 约束（来自 D1、D2 和数据要求） | 继续自写清单（现状） | 采用 DuckLake（SQLite catalog） |
|---|---|---|
| 服务是唯一写者，多读者，服务关闭时也能读 | 已满足：读者按清单读不可变文件 | 满足：官方推荐用法，实测 0 错误；读者以 `READ_ONLY` ATTACH 即可，不依赖服务 |
| 读者技术栈 | pyarrow 或 DuckDB 都能直读清单列出的文件 | **必须**通过 DuckDB 的 `ducklake` 和 `sqlite` 扩展，或另一个规范实现。naive 直读会读出重复行、漏掉 inlined 行 |
| 每根 K 线收盘就追加 | ADR-0011 目前每次快照冻结才发布一次；要做到逐根发布，得自己处理小文件 | inlining 原生支持；提交约 10 ms（实测） |
| 按月压实大文件 | 要自写；当前 `compaction.py` 只做图表序列整理，不做文件压实 | 内置 flush、merge、expire、cleanup 和 `CHECKPOINT`，可分层、可限量；压实时不影响读者（实测） |
| 原子发布与崩溃恢复 | 自写的 staging 质检 → fsync → rename → 短事务，语义完全可控 | 快照提交是原子的，崩溃只留孤儿文件，可清理（实测）。**没有内置的发布前质检**，质检要在写入前自己做 |
| Decimal(38,18) 精确 | 自己定义 Parquet schema | 实测精确往返；SQLite inlined 里存成 TEXT |
| `ts_available <= as_of` 防前视 | 行级过滤加自己的文件剪枝 | 行级过滤不变；文件级 min/max 剪枝自动生效（实测 402 个文件只读 1–2 个） |
| 还原当时看到的版本 | 依赖 `ts_committed` 和 revision 行；ADR-0011 的游标目前不接受迟到的 revision | 只追加的 revision 行同样可行；另外有快照时间旅行。前提是不过期快照 |
| 去重和唯一性 | 自己控制 | 不支持主键或唯一约束，写者必须自己去重 |
| 版本耦合 | 只依赖 Parquet 格式 | 所有读写进程最好固定同一个 DuckDB/ducklake 版本（有 #1860、#1924 的先例）。推断：Talaria 的进程共用一个 `.venv`，这一点可控，但每次升级 DuckDB 都要先测 catalog |
| 运行风险 | 自写代码的缺陷由自己承担 | SQLite catalog 的锁问题 2026 年修过三次；还有实测发现的 flush 静默回滚坑 |

决定性事实（按影响排序）：

1. **读路径**：采用 DuckLake 后，SDK、Notebook 和回测必须经过 DuckDB。DuckDB 本来就可用，所以这不是阻碍；但“pyarrow 直读清单文件”这条路径会消失。[K17]、实测 A.5
2. **单写者约束正好避开了已知的锁缺陷**：#1798 一类问题由并发写事务重叠触发，而 D1 本来就只有一个写者。[K1]、[K2]、issue
3. **压实、时间旅行、剪枝、孤儿清理是现成的**：这些正是自写方案在“逐根追加 + 按月压实”下还没写、而且最容易出错的部分。[K5]–[K8]、[K12]
4. **快照时间不等于 `ts_available`**：无论选哪条路，防前视都要靠行级时间戳。DuckLake 只能替代清单和压实层，替代不了数据中心的时间语义。[K10]
5. **版本兼容需要自己管**：规范承诺向后兼容，但新写旧读的事故已出现过两次；采用后需要把 DuckDB 版本固定和升级测试纳入流程。[B1]、[K20]、#1860、#1924

可能的折中（推断，供决策参考）：如果采用 DuckLake，自写代码只保留“写入前质检 + 单写者串行提交 + 维护调度（`CHECKPOINT` 节奏、不过期快照或只过期很老的快照）”，原来的 `data_manifest.sqlite` 和 generation 逻辑由 DuckLake catalog 取代。如果继续自写，现有发布协议之外还需要补三样：逐根追加时的小文件策略、按月压实及其与读者的并发协议、revision 行的追加路径。

## 10. 没有查到一手资料的问题

- SQLite catalog 在本机多进程高频读写时的官方性能数字：官方只给了 PostgreSQL 和 DuckDB catalog 的数字。[B2]、[B3]
- SQLite catalog 为什么用默认的 rollback journal 而不是 WAL，以及改成 WAL 是否受支持：文档没有提，本文只实测到 `journal_mode=delete`。
- `SELECT … FROM ducklake_flush_inlined_data(...)` 配合 `fetchone()` 时 flush 静默回滚的根因：官方 issue 里没有找到，是否属于预期行为不明。
- Parquet 写入时是否做 fsync，以及 catalog 提交前文件是否保证落盘：文档没有说明，扩展源码里也没有找到显式的 fsync 调用（仅检索了 `src/`，没有深入 DuckDB 的文件系统层），本文不作结论。
- 规范 1.x 系列内部的前向兼容承诺：旧扩展读新 catalog 已知不可行，官方没有书面的兼容矩阵覆盖 1.1 和 DuckDB 2.0（`compatibility_matrix.csv` 只列到 1.5.2 / 1.0）。[K23]

## 资料

### DuckLake 文档（`duckdb/ducklake-web` `f0ccdb7b`）

- [K1] [Choosing a Catalog Database](https://ducklake.select/docs/stable/duckdb/usage/choosing_a_catalog_database)
- [K2] [Conflict Resolution](https://ducklake.select/docs/stable/duckdb/advanced_features/conflict_resolution)
- [K3] [Transactions](https://ducklake.select/docs/stable/duckdb/advanced_features/transactions)
- [K4] [Configuration](https://ducklake.select/docs/stable/duckdb/usage/configuration)
- [K5] [Data Inlining](https://ducklake.select/docs/stable/duckdb/advanced_features/data_inlining)
- [K6] [Merge Adjacent Files](https://ducklake.select/docs/stable/duckdb/maintenance/merge_adjacent_files)
- [K7] [Expire Snapshots](https://ducklake.select/docs/stable/duckdb/maintenance/expire_snapshots)
- [K8] [Cleanup of Files](https://ducklake.select/docs/stable/duckdb/maintenance/cleanup_of_files)
- [K9] [Checkpoint](https://ducklake.select/docs/stable/duckdb/maintenance/checkpoint)；[Recommended Maintenance](https://ducklake.select/docs/stable/duckdb/maintenance/recommended_maintenance)
- [K10] [Time Travel](https://ducklake.select/docs/stable/duckdb/usage/time_travel)
- [K11] [Snapshots](https://ducklake.select/docs/stable/duckdb/usage/snapshots)
- [K12] [Partitioning](https://ducklake.select/docs/stable/duckdb/advanced_features/partitioning)
- [K13] [Sorted Tables](https://ducklake.select/docs/stable/duckdb/advanced_features/sorted_tables)
- [K14] [Unsupported Features](https://ducklake.select/docs/stable/duckdb/unsupported_features)；[Introduction](https://ducklake.select/docs/stable/duckdb/introduction)
- [K15] [Specification 1.1: Introduction](https://ducklake.select/docs/stable/specification/introduction)
- [K16] [Specification: Data Types](https://ducklake.select/docs/stable/specification/data_types)（含 SQLite/PostgreSQL inlining 类型映射）
- [K17] [Specification: Queries](https://ducklake.select/docs/stable/specification/queries)；[ducklake_data_file](https://ducklake.select/docs/stable/specification/tables/ducklake_data_file)
- [K19] [Backup and Recovery](https://ducklake.select/docs/stable/duckdb/guides/backups_and_recovery)
- [K20] [Upgrading to DuckLake 1.1](https://ducklake.select/docs/stable/duckdb/migrations/upgrading_to_ducklake_1_1)；[Troubleshooting](https://ducklake.select/docs/stable/duckdb/guides/troubleshooting)
- [K21] [FAQ](https://ducklake.select/faq)
- [K22] [Development Roadmap](https://ducklake.select/roadmap)（October 2026）
- [K23] [Release Calendar](https://ducklake.select/release_calendar)；数据源 [`_data/past_releases.csv`](https://github.com/duckdb/ducklake-web/blob/f0ccdb7b651836c75d8608d9f11eacea8b787482/_data/past_releases.csv)、[`_data/upcoming_releases.csv`](https://github.com/duckdb/ducklake-web/blob/f0ccdb7b651836c75d8608d9f11eacea8b787482/_data/upcoming_releases.csv)、[`_data/compatibility_matrix.csv`](https://github.com/duckdb/ducklake-web/blob/f0ccdb7b651836c75d8608d9f11eacea8b787482/_data/compatibility_matrix.csv)

### 官方博客

- [B1] [DuckLake 1.0: The Lakehouse Format Built on SQL Reaches Production-Readiness](https://ducklake.select/2026/04/13/ducklake-10/)（2026-04-13）
- [B2] [What's New in DuckLake 1.1](https://ducklake.select/2026/10/21/ducklake-11/)（2026-10-21）
- [B3] [Data Inlining in DuckLake: Unlocking Streaming for Data Lakes](https://ducklake.select/2026/04/02/data-inlining-in-ducklake/)
- [B4] [The DuckLake Spec Is so Simple, Even a Clanker Can Build One for Dataframes](https://ducklake.select/2026/05/04/ducklake-dataframe/)
- [B5] [Bringing DuckLake to DataFusion](https://ducklake.select/2026/07/29/bringing-ducklake-to-datafusion/)

### 源码与发布（`duckdb/ducklake`）

- [G1] [LICENSE（MIT）](https://github.com/duckdb/ducklake/blob/9c5533b096190d9cd5cf11f53c47b76f4002d4af/LICENSE)
- [G2] [src/include/common/ducklake_options.hpp L42：`busy_timeout = 5000`](https://github.com/duckdb/ducklake/blob/9c5533b096190d9cd5cf11f53c47b76f4002d4af/src/include/common/ducklake_options.hpp#L42)（`08089b16` 同样在 L42）
- [G3] [src/storage/ducklake_transaction.cpp L1540–1555：`RetryOnError`](https://github.com/duckdb/ducklake/blob/9c5533b096190d9cd5cf11f53c47b76f4002d4af/src/storage/ducklake_transaction.cpp#L1540-L1555)
- [G4] [src/storage/ducklake_storage.cpp L144–152：时点 ATTACH 强制只读，传递 busy_timeout](https://github.com/duckdb/ducklake/blob/9c5533b096190d9cd5cf11f53c47b76f4002d4af/src/storage/ducklake_storage.cpp#L144-L152)
- [D1] [DuckDB releases：v2.0.0（2026-10-21）、v2.0.1（2026-11-20）](https://github.com/duckdb/duckdb/releases)
- issue：[#128](https://github.com/duckdb/ducklake/issues/128)、[#1309](https://github.com/duckdb/ducklake/issues/1309)、[#1798](https://github.com/duckdb/ducklake/issues/1798)、[#1825](https://github.com/duckdb/ducklake/issues/1825)、[#1860](https://github.com/duckdb/ducklake/issues/1860)、[#1924](https://github.com/duckdb/ducklake/issues/1924)、[#1942](https://github.com/duckdb/ducklake/issues/1942)、[#1186](https://github.com/duckdb/ducklake/issues/1186)、[#1867](https://github.com/duckdb/ducklake/issues/1867)；PR [#1420](https://github.com/duckdb/ducklake/pull/1420)

## 附录 A：实验脚本与输出

脚本放在会话临时目录，数据放在 `mktemp -d` 生成的目录下的 `exp/`，都不在仓库里。运行方式统一为：

```bash
uv run --no-project --python .venv/bin/python \
  --with duckdb==2.0.1 --with pyarrow python -I <script> <args>
```

注：解释器是项目 `.venv` 里的 Python 3.15.2，但包安装在 uv 的一次性环境里，没有修改项目环境。pyarrow 版本为 26.0.1。

### A.1 环境

输出：
```text
3.15.2 (main, Nov 11 2026, 00:12:23) [Clang 23.1.3 ]
2.0.1
[('ducklake', '08089b16dc', 'REPOSITORY'), ('sqlite_scanner', 'ce0987fda4', 'REPOSITORY')]
# setup.py 输出（场景 A4）：
[('sqlite', '08089b16dc', '<tmp>/exp/A4/data/')]
[('version', '1.1')]
```

### A.2 公共连接与建表

`common.py`：
```python
import duckdb, os, sys
def connect(work, inline_limit=None, read_only=False):
    c = duckdb.connect()
    c.execute("LOAD ducklake; LOAD sqlite;")
    opts = [f"DATA_PATH '{work}/data/'"]
    if inline_limit is not None:
        opts.append(f"DATA_INLINING_ROW_LIMIT {inline_limit}")
    if read_only:
        opts.append("READ_ONLY")
    c.execute(f"ATTACH 'ducklake:sqlite:{work}/catalog.sqlite' AS lake ({', '.join(opts)})")
    return c
```

`setup.py`：
```python
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
from common import connect
work = sys.argv[1]
os.makedirs(f"{work}/data", exist_ok=True)
c = connect(work)
c.execute("""CREATE TABLE lake.bars (
  symbol VARCHAR, ts_event BIGINT, ts_available BIGINT, ts_ingested BIGINT, ts_committed BIGINT,
  ts_event_t TIMESTAMP, revision INTEGER, batch_id BIGINT,
  open DECIMAL(38,18), high DECIMAL(38,18), low DECIMAL(38,18), close DECIMAL(38,18), volume DECIMAL(38,18))
  PARTITIONED BY (epoch_month(ts_event_t)) SORTED BY (symbol, ts_event)""")
print(c.execute("FROM lake.settings()").fetchall())
print(c.execute("SELECT option_name, value FROM lake.options() WHERE option_name IN ('version','data_inlining_row_limit','target_file_size')").fetchall())
```

### A.3 一个写者 + 两个读者（场景 A：inlining 开；场景 B：inlining 关）

`writer.py`（最终版；第一次运行时 flush 用的是 `fetchone()`，导致 flush 静默丢失，见 A.6）：
```python
# usage: writer.py WORK SECONDS ROWS_PER_COMMIT INLINE_LIMIT FLUSH_EVERY_S OUT
import sys, os, time, json
sys.path.insert(0, os.path.dirname(__file__))
from common import connect
from decimal import Decimal
work, secs, k, lim, flush_every, out = sys.argv[1], float(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4]), float(sys.argv[5]), sys.argv[6]
c = connect(work, inline_limit=lim)
start = time.time(); bid = int(c.execute("SELECT coalesce(max(batch_id),0) FROM lake.bars").fetchone()[0]); lat = []; errs = []; last_flush = start; flushes = []
base_ms = 1767225600000  # 2026-01-01
while time.time() - start < secs:
    bid += 1
    rows = []
    for i in range(k):
        ts = base_ms + (bid * k + i) * 60000 * 60 * 24  # spread across days/months
        px = Decimal("12345.123456789012345678") + Decimal(bid) / Decimal(1000)
        rows.append((f"S{i}", ts, ts + 60000, ts + 60001, ts + 60002, bid, px, px, px, px, Decimal("0.000000000000000001") * bid))
    t0 = time.perf_counter()
    try:
        c.execute("BEGIN")
        c.executemany("""INSERT INTO lake.bars VALUES (?, ?, ?, ?, ?, make_timestamp(?::BIGINT*1000), 0, ?, ?, ?, ?, ?, ?)""",
                      [(r[0], r[1], r[2], r[3], r[4], r[1], r[5], r[6], r[7], r[8], r[9], r[10]) for r in rows])
        c.execute("COMMIT")
        lat.append((time.perf_counter() - t0) * 1000)
    except Exception as e:
        errs.append(f"{type(e).__name__}: {e}"[:300])
        try: c.execute("ROLLBACK")
        except Exception: pass
        bid -= 1
    if flush_every > 0 and time.time() - last_flush > flush_every:
        t0 = time.perf_counter()
        try:
            s0 = c.execute("SELECT max(snapshot_id) FROM lake.snapshots()").fetchone()[0]
            f = c.execute("SELECT sum(rows_flushed) FROM ducklake_flush_inlined_data('lake')").fetchall()[0][0]  # fetchall: see appendix, fetchone silently lost the flush
            s1 = c.execute("SELECT max(snapshot_id) FROM lake.snapshots()").fetchone()[0]
            m = c.execute("SELECT sum(files_processed), sum(files_created) FROM ducklake_merge_adjacent_files('lake')").fetchall()[0] if os.environ.get("MERGE","1")=="1" else (None,None)
            s2 = c.execute("SELECT max(snapshot_id) FROM lake.snapshots()").fetchone()[0]
            nf = c.execute("SELECT count(*) FROM __ducklake_metadata_lake.ducklake_data_file WHERE end_snapshot IS NULL").fetchone()[0]
            flushes.append({"flushed": str(f), "merge": [str(x) for x in m], "snaps": [s0, s1, s2], "live_files": nf, "ms": round((time.perf_counter()-t0)*1000,1)})
        except Exception as e:
            errs.append(f"maint {type(e).__name__}: {e}"[:300])
        last_flush = time.time()
    time.sleep(max(0, 1.0 / 4 - (time.perf_counter() - t0)))  # ~4 commits/s
lat.sort()
res = {"commits": len(lat), "errors": len(errs), "err_samples": errs[:5], "p50_ms": round(lat[len(lat)//2],1) if lat else None,
       "p99_ms": round(lat[int(len(lat)*0.99)],1) if lat else None, "max_ms": round(lat[-1],1) if lat else None, "last_batch": bid, "maint": flushes}
json.dump(res, open(out, "w"), indent=1); print(json.dumps(res))
```

`reader.py`：
```python
# usage: reader.py WORK SECONDS MODE(persist|reattach) K OUT
import sys, os, time, json
sys.path.insert(0, os.path.dirname(__file__))
from common import connect
from decimal import Decimal
work, secs, mode, k, out = sys.argv[1], float(sys.argv[2]), sys.argv[3], int(sys.argv[4]), sys.argv[5]
start = time.time(); n = 0; errs = []; incons = []; lat = []; c = None; seen_max = 0; types = None
while time.time() - start < secs:
    t0 = time.perf_counter()
    try:
        if c is None or mode == "reattach":
            if c is not None: c.close()
            c = connect(work, read_only=True)
        c.execute("BEGIN")
        snap = c.execute("FROM lake.current_snapshot()").fetchone()[0]
        cnt, nb, mx, sclose = c.execute("SELECT count(*), count(DISTINCT batch_id), max(batch_id), sum(close) FROM lake.bars").fetchone()
        # per-batch completeness and exact decimal check
        bad = c.execute("SELECT count(*) FROM (SELECT batch_id, count(*) n FROM lake.bars GROUP BY batch_id HAVING n <> ?)", [k]).fetchone()[0]
        row = c.execute("SELECT close, volume FROM lake.bars WHERE batch_id = (SELECT max(batch_id) FROM lake.bars) LIMIT 1").fetchone()
        c.execute("COMMIT")
        lat.append((time.perf_counter() - t0) * 1000)
        n += 1
        if types is None and row: types = [type(row[0]).__name__, str(row[0]), str(row[1])]
        if mx is not None:
            exp_sum = sum((Decimal("12345.123456789012345678") + Decimal(b) / Decimal(1000)) * k for b in range(1, mx + 1)) if nb == mx else None
            if cnt != nb * k or bad != 0 or nb != mx or (exp_sum is not None and sclose != exp_sum):
                incons.append({"snap": snap, "cnt": cnt, "nb": nb, "mx": mx, "bad": bad})
            if mx < seen_max: incons.append({"went_back": [seen_max, mx]})
            seen_max = max(seen_max, mx)
    except Exception as e:
        errs.append(f"{type(e).__name__}: {e}"[:300])
        try: c.execute("ROLLBACK")
        except Exception: c = None
    time.sleep(0.05)
lat.sort()
res = {"mode": mode, "reads": n, "errors": len(errs), "err_samples": list(dict.fromkeys(errs))[:5], "inconsistent": len(incons), "incons_samples": incons[:5],
       "p50_ms": round(lat[len(lat)//2],1) if lat else None, "p99_ms": round(lat[int(len(lat)*0.99)],1) if lat else None, "max_batch_seen": seen_max, "py_type_sample": types}
json.dump(res, open(out, "w"), indent=1); print(json.dumps(res))
```

`run.sh`（参数：场景名、inline 行数上限）：
```bash
#!/bin/bash
S=<scripts>
E=<tmp>/exp
PY=(uv run --no-project --python .venv/bin/python --with duckdb==2.0.1 --with pyarrow python -I)
name=$1; lim=$2; W=$E/$name; mkdir -p $W
echo "=== scenario $name inline=$lim"
"${PY[@]}" $S/setup.py $W 2>&1 | tail -2
"${PY[@]}" $S/writer.py $W 60 5 $lim 20 $W/w.json > $W/w.log 2>&1 &
sleep 3
"${PY[@]}" $S/reader.py $W 55 persist 5 $W/r1.json > $W/r1.log 2>&1 &
"${PY[@]}" $S/reader.py $W 55 reattach 5 $W/r2.json > $W/r2.log 2>&1 &
wait
cat $W/w.json; echo; cat $W/r1.json; echo; cat $W/r2.json; echo
echo "parquet files: $(find $W/data -name '*.parquet' | wc -l)"
ls -la $W
```

场景 A（`run.sh A4 10`）输出：
```json
writer: {"commits": 236, "errors": 0, "err_samples": [], "p50_ms": 10.0, "p99_ms": 52.3, "max_ms": 56.2, "last_batch": 236, "maint": [{"flushed": "400", "merge": ["None", "None"], "snaps": [81, 82, 82], "live_files": 14, "ms": 28.8}, {"flushed": "395", "merge": ["2", "1"], "snaps": [161, 162, 163], "live_files": 27, "ms": 32.6}]}
reader persist: {"mode": "persist", "reads": 827, "errors": 0, "err_samples": [], "inconsistent": 0, "incons_samples": [], "p50_ms": 9.9, "p99_ms": 35.4, "max_batch_seen": 228, "py_type_sample": ["Decimal", "12345.135456789012345678", "1.2E-17"]}
reader reattach: {"mode": "reattach", "reads": 461, "errors": 0, "err_samples": [], "inconsistent": 0, "incons_samples": [], "p50_ms": 64.3, "p99_ms": 87.9, "max_batch_seen": 228, "py_type_sample": ["Decimal", "12345.135456789012345678", "1.2E-17"]}
```

场景 B（`run.sh B4 0`）输出：
```json
writer: {"commits": 235, "errors": 0, "err_samples": [], "p50_ms": 24.6, "p99_ms": 91.2, "max_ms": 95.4, "last_batch": 235, "maint": [{"flushed": "None", "merge": ["400", "14"], "snaps": [81, 81, 82], "live_files": 14, "ms": 116.3}, {"flushed": "None", "merge": ["401", "14"], "snaps": [162, 162, 163], "live_files": 27, "ms": 124.6}]}
reader persist: {"mode": "persist", "reads": 612, "errors": 0, "err_samples": [], "inconsistent": 0, "incons_samples": [], "p50_ms": 33.2, "p99_ms": 74.3, "max_batch_seen": 227, "py_type_sample": ["Decimal", "12345.135456789012345678", "1.2E-17"]}
reader reattach: {"mode": "reattach", "reads": 378, "errors": 0, "err_samples": [], "inconsistent": 0, "incons_samples": [], "p50_ms": 88.1, "p99_ms": 159.0, "max_batch_seen": 227, "py_type_sample": ["Decimal", "12345.135456789012345678", "1.2E-17"]}
```

### A.4 时间旅行与 revision

`timetravel.py`：
```python
import sys, os, time
sys.path.insert(0, os.path.dirname(__file__))
from common import connect
work = sys.argv[1]; os.makedirs(f"{work}/data", exist_ok=True)
c = connect(work)
c.execute("""CREATE TABLE lake.bars (symbol VARCHAR, ts_event BIGINT, ts_available BIGINT, revision INTEGER, close DECIMAL(38,18))""")
# revision 0 of bar 1000 visible at ts_available=1060
c.execute("INSERT INTO lake.bars VALUES ('BTC', 1000, 1060, 0, 100.000000000000000001)")
s_rev0 = c.execute("SELECT id FROM lake.current_snapshot()").fetchall()[0][0]
time.sleep(1.1); t_between = c.execute("SELECT now()").fetchall()[0][0]; time.sleep(1.1)
# a late correction: revision 1 of the same bar, available at 5000
c.execute("INSERT INTO lake.bars VALUES ('BTC', 1000, 5000, 1, 100.5)")
s_rev1 = c.execute("SELECT id FROM lake.current_snapshot()").fetchall()[0][0]
q = "SELECT revision, close FROM lake.bars {at} WHERE ts_available <= {asof} QUALIFY row_number() OVER (PARTITION BY symbol, ts_event ORDER BY revision DESC)=1"
print("snapshots rev0/rev1:", s_rev0, s_rev1)
print("latest, as_of=9999         :", c.execute(q.format(at="", asof=9999)).fetchall())
print("latest, as_of=2000 (row ts):", c.execute(q.format(at="", asof=2000)).fetchall())
print(f"AT VERSION {s_rev0}, as_of=9999:", c.execute(q.format(at=f"AT (VERSION => {s_rev0})", asof=9999)).fetchall())
print("AT TIMESTAMP between       :", c.execute(q.format(at=f"AT (TIMESTAMP => TIMESTAMPTZ '{t_between}')", asof=9999)).fetchall())
# flush + merge, then time travel again
print("flush:", c.execute("CALL ducklake_flush_inlined_data('lake')").fetchall())
print("merge:", c.execute("CALL ducklake_merge_adjacent_files('lake')").fetchall())
print(f"after flush AT VERSION {s_rev0}:", c.execute(q.format(at=f"AT (VERSION => {s_rev0})", asof=9999)).fetchall())
# expire old snapshots: time travel to expired snapshot
print("expire:", c.execute(f"CALL ducklake_expire_snapshots('lake', versions => [{s_rev0}])").fetchall())
try:
    print("AT expired version:", c.execute(q.format(at=f"AT (VERSION => {s_rev0})", asof=9999)).fetchall())
except Exception as e:
    print("AT expired version error:", str(e)[:200])
try:
    print("future timestamp:", c.execute("SELECT count(*) FROM lake.bars AT (TIMESTAMP => now() + INTERVAL 1 hour)").fetchall())
except Exception as e:
    print("future timestamp error:", str(e)[:200])
# pushdown: EXPLAIN shows filters / file pruning
print(c.execute("EXPLAIN SELECT * FROM lake.bars WHERE ts_event BETWEEN 0 AND 2000").fetchall()[0][1][:900])
```

输出：
```text
snapshots rev0/rev1: 2 3
latest, as_of=9999         : [(1, Decimal('100.500000000000000000'))]
latest, as_of=2000 (row ts): [(0, Decimal('100.000000000000000001'))]
AT VERSION 2, as_of=9999: [(0, Decimal('100.000000000000000001'))]
AT TIMESTAMP between       : [(0, Decimal('100.000000000000000001'))]
flush: [('main', 'bars', 2)]
merge: []
after flush AT VERSION 2: [(0, Decimal('100.000000000000000001'))]
expire: [(2, datetime.datetime(2026, 11, 20, 12, 35, 12, 51330, tzinfo=zoneinfo.ZoneInfo(key='America/New_York')), 1, {'inlined_insert': ['1']}, None, None, None)]
AT expired version error: Invalid Input Error: No snapshot found at version 2
future timestamp error: Invalid Input Error: Cannot time travel to a future timestamp: 2026-11-20 18:35:14.357225+00
(EXPLAIN 输出略)
```

### A.5 pyarrow 直读与文件剪枝

`inspect_files.py`：
```python
import sys, os, glob, sqlite3
sys.path.insert(0, os.path.dirname(__file__))
from common import connect
import pyarrow.parquet as pq, pyarrow.dataset as ds
work = sys.argv[1]
print("sqlite journal_mode:", sqlite3.connect(f"{work}/catalog.sqlite").execute("PRAGMA journal_mode").fetchone())
c = connect(work, read_only=True)
live = c.execute("SELECT path, record_count, partial_max, begin_snapshot FROM __ducklake_metadata_lake.ducklake_data_file WHERE end_snapshot IS NULL ORDER BY record_count DESC").fetchall()
allf = c.execute("SELECT count(*), sum(record_count) FROM __ducklake_metadata_lake.ducklake_data_file").fetchone()
print("live files:", len(live), "rows in live files:", sum(r[1] for r in live), "| all catalog files (incl. ended):", allf)
print("table count:", c.execute("SELECT count(*) FROM lake.bars").fetchone(), "inlined rows live:", c.execute("SELECT count(*) FROM __ducklake_metadata_lake.ducklake_inlined_data_1_1 WHERE _ducklake_end_snapshot IS NULL").fetchone())
files = glob.glob(f"{work}/data/**/*.parquet", recursive=True)
tbl = ds.dataset(files, format="parquet").to_table() if files else None
print("pyarrow naive glob over data dir: files", len(files), "rows", tbl.num_rows if tbl is not None else 0)
# inspect a merged (partial) file
merged = [r for r in live if r[2] is not None]
target = None
if merged:
    p = merged[0][0]
    target = p if os.path.isabs(p) else None
    if target is None:
        target = [f for f in files if f.endswith(os.path.basename(p))][0]
elif live:
    target = [f for f in files if f.endswith(os.path.basename(live[0][0]))][0]
if target:
    f = pq.ParquetFile(target)
    print("sample file:", os.path.relpath(target, work), "partial_max:", merged[0][2] if merged else None)
    sch = f.schema_arrow
    for fld in sch:
        print("   ", fld.name, fld.type, (fld.metadata or {}).get(b'PARQUET:field_id'))
    rg = f.metadata.row_group(0)
    for i in range(rg.num_columns):
        col = rg.column(i)
        if col.path_in_schema in ("ts_event", "close"):
            print("    stats", col.path_in_schema, col.statistics.min if col.statistics else None, col.statistics.max if col.statistics else None)
    t = f.read()
    print("    first row:", {k: v[0] for k, v in t.slice(0,1).to_pydict().items()})
```

场景 B 输出：
```text
sqlite journal_mode: ('delete',)
live files: 402 rows in live files: 1175 | all catalog files (incl. ended): (402, 1175)
table count: (1175,) inlined rows live: (0,)
pyarrow naive glob over data dir: files 1203 rows 1984
sample file: data/main/bars/epoch_month=678/ducklake-01a1fa05-6b49-7f8a-ac9c-f88c7571a3af.parquet partial_max: 43
    symbol string b'1'
    ts_event int64 b'2'
    ts_available int64 b'3'
    ts_ingested int64 b'4'
    ts_committed int64 b'5'
    ts_event_t timestamp[us] b'6'
    revision int32 b'7'
    batch_id int64 b'8'
    open decimal128(38, 18) b'9'
    high decimal128(38, 18) b'10'
    low decimal128(38, 18) b'11'
    close decimal128(38, 18) b'12'
    volume decimal128(38, 18) b'13'
    _ducklake_internal_row_id int64 b'2147483540'
    _ducklake_internal_snapshot_id int64 b'2147483539'
    stats ts_event 1782864000000 1785456000000
    stats close 12345.159456789012345678 12345.165456789012345678
    first row: {'symbol': 'S0', 'ts_event': 1783209600000, 'ts_available': 1783209660000, 'ts_ingested': 1783209660001, 'ts_committed': 1783209660002, 'ts_event_t': datetime.datetime(2026, 7, 5, 0, 0), 'revision': 0, 'batch_id': 37, 'open': Decimal('12345.160456789012345678'), 'high': Decimal('12345.160456789012345678'), 'low': Decimal('12345.160456789012345678'), 'close': Decimal('12345.160456789012345678'), 'volume': Decimal('3.7E-17'), '_ducklake_internal_row_id': 180, '_ducklake_internal_snapshot_id': 38}
```

场景 A 输出：
```text
sqlite journal_mode: ('delete',)
live files: 27 rows in live files: 795 | all catalog files (incl. ended): (27, 795)
table count: (1180,) inlined rows live: (385,)
pyarrow naive glob over data dir: files 29 rows 823
sample file: data/main/bars/epoch_month=696/ducklake-01a1fa04-cd3b-71c3-9acf-e254dc83f309.parquet partial_max: 154
    symbol string b'1'
    ts_event int64 b'2'
    ts_available int64 b'3'
    ts_ingested int64 b'4'
    ts_committed int64 b'5'
    ts_event_t timestamp[us] b'6'
    revision int32 b'7'
    batch_id int64 b'8'
    open decimal128(38, 18) b'9'
    high decimal128(38, 18) b'10'
    low decimal128(38, 18) b'11'
    close decimal128(38, 18) b'12'
    volume decimal128(38, 18) b'13'
    _ducklake_internal_row_id int64 b'2147483540'
    _ducklake_internal_snapshot_id int64 b'2147483539'
    stats ts_event 1830297600000 1832889600000
    stats close 12345.269456789012345678 12345.275456789012345678
    first row: {'symbol': 'S2', 'ts_event': 1830470400000, 'ts_available': 1830470460000, 'ts_ingested': 1830470460001, 'ts_committed': 1830470460002, 'ts_event_t': datetime.datetime(2028, 1, 3, 0, 0), 'revision': 0, 'batch_id': 146, 'open': Decimal('12345.269456789012345678'), 'high': Decimal('12345.269456789012345678'), 'low': Decimal('12345.269456789012345678'), 'close': Decimal('12345.269456789012345678'), 'volume': Decimal('1.46E-16'), '_ducklake_internal_row_id': 727, '_ducklake_internal_snapshot_id': 148}
```

`pruning.py`（场景 B 的 catalog）：
```python
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
from common import connect
c = connect(sys.argv[1], read_only=True)
for w in ['TRUE', 'ts_event BETWEEN 1782864000000 AND 1783000000000', 'ts_available <= 1770000000000', 'close > 12345.4']:
    p = c.execute(f'EXPLAIN ANALYZE SELECT count(*) FROM lake.bars WHERE {w}').fetchall()[0][1]
    print(w, '|', [l.strip(' │') for l in p.splitlines() if 'Files' in l or 'Filters' in l or 'ts_event' in l][:6])
```

输出（`TRUE` 和 `close > 12345.4` 两行没有文件读取记录，推断是直接用 catalog 统计回答的）：
```text
TRUE | []
ts_event BETWEEN 1782864000000 AND 1783000000000 | ['Filters:', '(ts_event >= 1782864000000) AND (ts_event <= 1783000000000)', 'Total Files Read: 1']
ts_available <= 1770000000000 | ['Filters: ts_available <= 1770000000000', 'Total Files Read: 2']
close > 12345.4 | []
```

### A.6 flush 配合 `fetchone()` 静默回滚

`fetch_test.py`：
```python
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
from common import connect
base = sys.argv[1]
for how in ["fetchall", "fetchone", "fetchone_then_close_conn", "CALL_fetchone"]:
    work = f"{base}/{how}"; os.makedirs(f"{work}/data", exist_ok=True)
    c = connect(work)
    c.execute("CREATE TABLE lake.t (a INTEGER, d DECIMAL(38,18)) PARTITIONED BY (a)")
    for i in range(3): c.execute("INSERT INTO lake.t VALUES (?, 1.5)", [i])
    if how == "fetchall": r = c.execute("SELECT sum(rows_flushed) FROM ducklake_flush_inlined_data('lake')").fetchall()
    elif how == "CALL_fetchone": r = c.execute("CALL ducklake_flush_inlined_data('lake')").fetchone()
    else: r = c.execute("SELECT sum(rows_flushed) FROM ducklake_flush_inlined_data('lake')").fetchone()
    if how != "fetchone_then_close_conn":
        c.execute("SELECT 1").fetchall()
    c.close()
    c2 = connect(work, read_only=True)
    nf = c2.execute("SELECT count(*) FROM __ducklake_metadata_lake.ducklake_data_file").fetchone()[0]
    files = sum(len(f) for _,_,f in os.walk(work+'/data'))
    print(f"{how:26} result={r} catalog_data_files={nf} files_on_disk={files}")
```

输出（复现两次，结果相同）：
```text
fetchall                   result=[(3,)] catalog_data_files=3 files_on_disk=3
fetchone                   result=(3,) catalog_data_files=0 files_on_disk=0
fetchone_then_close_conn   result=(3,) catalog_data_files=0 files_on_disk=0
CALL_fetchone              result=('main', 't', 3) catalog_data_files=3 files_on_disk=3
```

对照组 `fetch_test2.py`（原生 DuckDB 表）：
```python
import sys, os, duckdb
base = sys.argv[1]; os.makedirs(base, exist_ok=True)
p = f"{base}/native.duckdb"
c = duckdb.connect(p); c.execute("CREATE TABLE t (a INT)")
r = c.execute("INSERT INTO t SELECT range FROM range(5) RETURNING a").fetchone()
c.execute("SELECT 1").fetchall(); c.close()
print("native duckdb INSERT..RETURNING fetchone:", r, duckdb.connect(p, read_only=True).execute("SELECT count(*) FROM t").fetchone())
```

输出：
```text
native duckdb INSERT..RETURNING fetchone: (0,) (5,)
```

### A.7 崩溃与两个写者

`crash_writer.py`：
```python
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
from common import connect
work = sys.argv[1]
c = connect(work, inline_limit=0)
c.execute("INSERT INTO lake.t VALUES (0, 1.5)")  # one committed row
print("committed baseline", flush=True)
c.execute("BEGIN")
c.execute("INSERT INTO lake.t SELECT range::INT, 1.5 FROM range(300000000)")  # big write, killed mid-way
c.execute("COMMIT")
print("should not reach", flush=True)
```

`crash.sh`：
```bash
#!/bin/bash
S=<scripts>
W=<tmp>/exp/CR2; mkdir -p $W/data
PY=(uv run -q --no-project --python .venv/bin/python --with duckdb==2.0.1 python -I)
"${PY[@]}" -c "import sys; sys.path.insert(0,'$S'); from common import connect; connect('$W').execute('CREATE TABLE lake.t (a INTEGER, d DECIMAL(38,18))')"
"${PY[@]}" $S/crash_writer.py $W & 
for i in $(seq 1 200); do [ $(find $W/data -type f | wc -l) -ge 2 ] && break; sleep 0.1; done; sleep 0.5
pkill -9 -f crash_writer.py; wait
echo "files on disk after kill -9: $(find $W/data -type f | wc -l)  bytes: $(du -sk $W/data | cut -f1)KB"
ls -la $W
"${PY[@]}" -c "
import sys; sys.path.insert(0,'$S'); from common import connect
c=connect('$W')
print('count after crash:', c.execute('SELECT count(*) FROM lake.t').fetchall(), 'catalog files:', c.execute('SELECT count(*) FROM __ducklake_metadata_lake.ducklake_data_file').fetchall())
print('orphans dry run:', len(c.execute(\"CALL ducklake_delete_orphaned_files('lake', dry_run => true, cleanup_all => true)\").fetchall()))
c.execute('INSERT INTO lake.t VALUES (1, 2.5)'); print('write after crash ok, count:', c.execute('SELECT count(*) FROM lake.t').fetchall())
print('delete orphans:', len(c.execute(\"CALL ducklake_delete_orphaned_files('lake', cleanup_all => true)\").fetchall()))
"
echo "files on disk after cleanup: $(find $W/data -type f | wc -l)"
```

输出：
```text
committed baseline
crash.sh: line 8: 72003 Killed: 9               "${PY[@]}" $S/crash_writer.py $W
files on disk after kill -9:        2  bytes: 291480KB
count after crash: [(1,)] catalog files: [(1,)]
orphans dry run: 1
write after crash ok, count: [(2,)]
delete orphans: 1
files on disk after cleanup:        1
```

`two_writers.sh`（参数 `0`，即关闭 inlining）：
```bash
#!/bin/bash
S=<scripts>
W=<tmp>/exp/TW; mkdir -p $W
PY=(uv run -q --no-project --python .venv/bin/python --with duckdb==2.0.1 python -I)
"${PY[@]}" $S/setup.py $W > /dev/null
"${PY[@]}" $S/writer.py $W 30 5 $1 0 $W/w1.json > /dev/null 2>&1 &
"${PY[@]}" $S/writer.py $W 30 5 $1 0 $W/w2.json > /dev/null 2>&1 &
wait; cat $W/w1.json; echo; cat $W/w2.json; echo
"${PY[@]}" $S/probe.py $W "SELECT count(*), count(DISTINCT batch_id) FROM lake.bars"
```

输出：
```text
w1: {"commits": 116, "errors": 0, "err_samples": [], "p50_ms": 105.3, "p99_ms": 148.0, "max_ms": 148.3, "last_batch": 116, "maint": []}
w2: {"commits": 116, "errors": 0, "err_samples": [], "p50_ms": 32.8, "p99_ms": 152.6, "max_ms": 154.7, "last_batch": 116, "maint": []}
>> SELECT count(*), count(DISTINCT batch_id) FROM lake.bars
   (1160, 116)
```
