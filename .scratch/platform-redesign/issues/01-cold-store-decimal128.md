# 01 — 冷库数值格式迁移到按标的精度的 decimal128

**What to build:** 冷库里的 K 线和衍生品不再以十进制字符串存储，改为每个标的按自身精度存 `decimal128(18, s)`（物理 INT64），并补齐时点列。已有冷库数据可以用一条命令一次性迁移，迁移后快照、图表、关键位、`evaluate-levels` 读到的数值与迁移前逐值相等。

**Blocked by:** None — can start immediately.

**Source of truth:** ADR-0017；`docs/architecture.md` §4.2。

**Status:** ready-for-agent

- [ ] 新写入的分区文件价格、数量、成交额为 `decimal128(18, 标的精度)`，Parquet 物理类型 INT64；精度取自 `InstrumentSpec`，读取方按文件元数据还原，不假设全库同一 scale。
- [ ] 列包含 `ts_event`、OHLCV、`turnover`、`ts_available`、`ts_ingested`、`ts_committed`（int64 毫秒）、`precision`（字典编码）、`revision`。
- [ ] 提供一次性迁移命令：读旧字符串 generation，写新格式，清单原子切换；迁移可重复执行（幂等），失败不破坏旧数据。
- [ ] 测试：同一批柱迁移前后读出的 `Decimal` 逐值相等；标的改精度后新旧分区混读正确；超出 18 位的值被拒绝并报错。
- [ ] 快照构建与现有冷库读取路径的测试保持通过。
