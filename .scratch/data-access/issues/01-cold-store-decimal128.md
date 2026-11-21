# 01 — 冷库数值格式迁移到按列精度的 decimal128

**What to build:** 冷库里的 K 线不再以十进制字符串存储，改为每个标的、每列按自身 scale 存 `decimal128(18, s)`（物理 INT64），并补齐时点列与修订列。已有冷库数据可以用一条命令一次性迁移，迁移后快照、图表、关键位、`evaluate-levels` 读到的数值与迁移前逐值相等。衍生品进入冷库在 08 做。

**Blocked by:** None — can start immediately.

**Source of truth:** ADR-0017、ADR-0018（修订与时点）；`docs/architecture.md` §4.2。

**Status:** ready-for-agent

- [ ] scale 按列：价格类取价格精度，数量类取数量精度，`turnover` 取两者之和，费率类固定 8；各列 scale 与 `InstrumentSpec` 版本写入 Parquet 元数据，读取方按元数据还原，不假设全库同一 scale。
- [ ] 小数位超出列 scale 或超出 18 位有效数字时整批拒绝并报错，不静默舍入。
- [ ] 列包含 `ts_event`、OHLCV、`turnover`、`ts_available`、`ts_ingested`、`ts_committed`（int64 毫秒）、`precision`（字典编码）、`revision`；同一 `ts_event` 的不同修订各占一行。
- [ ] 读取实现只有一份：先按 `ts_available <= as_of` 截断，再对每个 `ts_event` 取最大 `revision`；省略 `as_of` 取最新。
- [ ] 提供一次性迁移命令：读旧字符串 generation，写新格式，清单原子切换；迁移可重复执行（幂等），失败不破坏旧数据。
- [ ] 测试：同一批柱迁移前后读出的 `Decimal` 逐值相等，含仓库样本中成交额 `809038.73`；标的改精度后新旧分区混读正确；超出 scale 或 18 位的值被拒绝；同一柱两个修订在两个 `as_of` 下分别返回各自版本。
- [ ] 快照构建与现有冷库读取路径的测试保持通过。
