---
status: accepted
---

# 取数 SDK 按清单直读 Parquet，最新部分向常驻服务补齐；继续自写清单

图表、Notebook、策略和 agent 要拿到同一份、同一口径的数据，并且“一两行代码就能取到”。决定：

- **Python 取数 SDK** 按 `data_manifest.sqlite` 直接读 Parquet；冷库尚未写入的最新几根向常驻服务取，按时间戳拼接去重；服务未运行时只用本地数据。
- 所有查询支持 `as_of`：先按 `ts_available <= as_of` 截断，再对每个 `ts_event` 取最大 `revision`（ADR-0018）。参照项目都不提供这种截断，由本 SDK 实现，图表、回测、组件运行时共用同一实现。
- 服务补齐的最新部分带 `confirmed` 与 `revision`。策略和组件只看到已收盘的柱：收盘时刻已过但确认消息未到的柱不算收盘；图表可以显示进行中的柱。
- SDK 给策略和指标返回精确的 `Decimal` 契约对象；`.to_frame()` 给 float DataFrame 供研究使用。
- 常驻服务的 HTTP 接口新增按时间范围取数；DSH 工具与 SDK 共用同一套契约。
- **实时订阅语义**：图表页面满了丢最旧。策略订阅不静默丢弃，队列满则断开；交付单位是一个柱修订，带服务分配的变更序号 `(epoch, seq)`。客户端重连时带上最后收到的序号：服务先挂上实时订阅并缓冲，再从保留的变更日志补发更大序号的修订，最后放出缓冲并按序号去重。序号超出保留范围或服务重启换了 `epoch` 时，服务返回需要重新同步，客户端按范围接口重取并按 `(ts_event, revision)` 去重。这样断线期间对旧柱的修订、恰好在切换时收盘的柱都不会漏。
- **订阅按使用方持有**：上游订阅配额按 `(标的, 周期)` 记在各使用方（图表页、预警、账户进程、自选）名下。账户进程的订阅优先保留，配额不足时明确拒绝而不是挤掉别人；被挤掉的使用方收到通知。
- **清单继续自写**（在现有 `cold_store` 上扩展），文件结构保持可迁移，暂不采用 DuckLake。

## Considered Options

- **策略订阅按最后收到的 `ts_event` 向后补齐**：实现最简单，但会漏掉断线期间对更早柱的修订，以及补齐与重新订阅之间收盘的柱。否决。

- **所有读取都走常驻服务**：口径集中，但大批量历史要经 HTTP 序列化，服务停了 Notebook 就取不到数，回测也受服务负载影响。否决；Nautilus、LEAN、ArcticDB、OpenBB 都由调用方自己读存储。
- **采用 DuckLake 作为表格式**：并发实验中一写两读无锁错误，但所有读者必须经 DuckDB（pyarrow 直读结果不对）、存在跨版本兼容事故（#1924 未修）、出现过 flush 静默回滚的未解释现象。否决，等其稳定后可迁移。
- **用 DuckDB 文件作为主存储**：单写者锁会让 Notebook 与服务互相阻塞。否决；DuckDB 仍可作为查询 Parquet 的工具。

依据：`docs/research/ducklake-evaluation.md`、`docs/research/data-access-and-numeric-types.md`、`docs/research/unified-strategy-runtime-sources.md`。
