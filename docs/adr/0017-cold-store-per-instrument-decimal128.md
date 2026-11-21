---
status: accepted
---

# 冷库价格与数量按标的精度存为 decimal128(18, s)，物理为 INT64

冷库原先把价格和数量存成十进制字符串（与 JSON 边界的 `DecimalStr` 一致），读取时逐个转 `Decimal`，大批量取数和转 float 都慢。决定：每个标的一个文件（K 线按 `标的/周期/月` 分区，逐笔与盘口按 `标的/日` 分区），价格、数量、成交额使用 `decimal128(18, 该标的精度)`，Parquet 物理类型为 INT64。

实测 100 万行：约 22 MB，读取 11 ms，转 float 11 ms；精确、无尾零。对比 `decimal128(38,18)` 41 MB、转 `Decimal` 1.41 s，且在 DuckDB 中 `close*volume` 会溢出；字符串 24 MB、转 `Decimal` 0.39 s。

列：`ts_event`、OHLCV 与 `turnover`、`ts_available`、`ts_ingested`、`ts_committed`（int64 毫秒）、`precision`（字典编码）、`revision`（预留）。

## Consequences

- 精度跟随标的规格（`InstrumentSpec` 的 tick/step），标的改精度时新分区用新精度，读取方按文件元数据还原，不能假设全库同一 scale。
- `Decimal` 仍是 Python 内的唯一权威表示（ADR-0010）；跨 JSON 边界仍用十进制字符串。只有 Parquet 存储层换表示。
- 现有字符串冷库需要一次性迁移。

## Considered Options

- **统一 `decimal128(38,18)`**：写起来省事，但体积翻倍、转换慢、在 DuckDB 中乘法溢出。否决。
- **int64 + 单独的 scale 列**：体积和速度相同，但其他工具读出来是无意义的整数。否决。
- **继续存字符串**：精确但慢，其他工具无法直接做数值运算。否决。
- **float64**：快，但不精确，违背 ADR-0010。否决。

依据：`docs/research/data-access-and-numeric-types.md`。
