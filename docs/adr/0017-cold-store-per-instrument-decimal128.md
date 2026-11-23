---
status: accepted
---

# 冷库数值按标的、按列精度存为 decimal128(18, s)，物理为 INT64

冷库原先把价格和数量存成十进制字符串（与 JSON 边界的 `DecimalStr` 一致），读取时逐个转 `Decimal`，大批量取数和转 float 都慢。决定：每个标的一个文件（K 线按 `标的/周期/月` 分区，逐笔与盘口按 `标的/日` 分区，衍生品按 `标的/类别/月` 分区），数值列使用 `decimal128(18, s)`，Parquet 物理类型为 INT64。

实测 100 万行：约 22 MB，读取 11 ms，转 float 11 ms；精确、无尾零。对比 `decimal128(38,18)` 41 MB、转 `Decimal` 1.41 s，且在 DuckDB 中 `close*volume` 会溢出；字符串 24 MB、转 `Decimal` 0.39 s。

## 每一列的 scale

scale 按列的含义定，不是全文件统一：

| 列类别 | scale | 例（BTC 永续：价格 1 位、数量 3 位） |
|---|---|---|
| 价格类（OHLC、标记价、指数价、买一卖一价、逐笔价） | 标的价格精度 | 1 |
| 数量类（成交量、持仓量、买一卖一量、逐笔量） | 标的数量精度 | 3 |
| 金额类（`turnover`） | 价格精度 + 数量精度 | 4，可无损表示 `809038.73` |
| 比率类（资金费率） | 固定 8 | `0.00005` |

每个文件在 Parquet 元数据中记录各列 scale 和来源 `InstrumentSpec` 版本。写入时值的小数位超过该列 scale 或超出 18 位有效数字，整批拒绝并报错，不静默舍入。

时点与元数据列：`ts_event`、`ts_available`、`ts_ingested`、`ts_committed`（int64 毫秒）、`precision`（字典编码）、`revision`（int64，见 ADR-0018）。

## Consequences

- 标的改精度时新分区用新精度，读取方按文件元数据还原，不能假设全库同一 scale。
- `Decimal` 仍是 Python 内的唯一权威表示（ADR-0010）；跨 JSON 边界仍用十进制字符串。只有 Parquet 存储层换表示。
- 现有字符串冷库需要一次性迁移，迁移验收要包括仓库样本中的成交额与资金费率。

## Considered Options

- **统一 `decimal128(38,18)`**：写起来省事，但体积翻倍、转换慢、在 DuckDB 中乘法溢出。否决。
- **所有数值列都用标的价格精度**：成交额和资金费率的小数位超出价格精度，存不下。否决。
- **int64 + 单独的 scale 列**：体积和速度相同，但其他工具读出来是无意义的整数。否决。
- **继续存字符串**：精确但慢，其他工具无法直接做数值运算。否决。
- **float64**：快，但不精确，违背 ADR-0010。否决。

依据：`docs/research/data-access-and-numeric-types.md`（仅本地）。
