---
status: accepted
---

# v1 指标核以纯 Python Decimal 为唯一权威，第三方指标库只作锁定依赖

架构规范原先写“底层复用 `TA-Lib` / `talipp`”。04 实现时实测：两个库在 Python 3.14 / macOS arm64 上可安装，但批量库与增量库对预热、空柱和断流的语义并不一致；若同时接入两套引擎，反而要维护一份“哪套是权威”的映射。v1 的硬约束是“指标只在 Python 实现一份，图表、预警与智能体消费同一结果”，因此选择纯 Python `Decimal` 算子核作为唯一权威。

## Considered Options

- **`TA-Lib` 批量 + `talipp` 增量双实现**：性能最好，但两套引擎的 warmup、NaN、NO_TRADES / FEED_GAP 处理需要逐指标对齐；一旦漂移，图表、预警和智能体可能看到不同数值。否决。
- **只接 `TA-Lib`，增量路径每次重算窗口**：实现简单，但实时柱每次都要重算整段，且 `TA-Lib` 的 `NaN` 前导与 `FEED_GAP` 语义仍需包装；跨平台 wheel 也不是所有环境都有。否决。
- **前端图表库或 JavaScript 指标库计算，Python 只做转发**：会重新引入多端实现，且与“图表离开守护进程无法自行计算指标”的契约冲突。否决。

## Consequences

- `core/src/talaria/foundation/operators/` 的纯 Python 实现是唯一数值来源；`get_indicators`、`compute_operator`、预警引擎和浏览器副图都消费它。
- `TA-Lib==0.8.3` 与 `talipp==2.7.0` 仍固定在 `core/pyproject.toml` 并登记许可，作为未来批量/流式优化的候选；v1 运行时不得用它们绕过黄金向量。
- 性能回退是已知代价；未来若接入适配器，必须先通过 `contracts/operator_vectors/` 的同一组黄金向量，且不得改变 `IndicatorSeries` 的语义。
- 本决策取代 ADR-0008 中“底层复用 `TA-Lib` / `talipp`”的运行时表述；依赖版本基线仍按 01 的核实结论保留。

## 追加：算法语义以 Pine v6 参考手册为准（2026-11，任务 03）

Pine v6 参考手册是 EMA、ATR、VWAP 数值语义的公开基准；实现仍是纯 Python `Decimal`，不引入 TradingView 私有接口，也不拿 TV 输出回填黄金向量。本次对齐三条语义与两处默认值：

- **EMA**：`ta.ema` 以第一根 close 作种子并立即输出，之后 `alpha*src + (1-alpha)*prev`，`alpha = 2/(N+1)`；`FEED_GAP` 重预热后视为新序列起点。
- **ATR**：`ta.atr = ta.rma(ta.tr(true), N)`。第一根 TR 取 `high - low` 并计入前 N 个 TR 的简单均值种子，之后按 `alpha*TR + (1-alpha)*prev` 递推，`alpha = 1/N`。
- **VWAP**：默认锚点改为 `session`（UTC 日界），第一个日界之前输出空，日界处清零后从当前 bar 重新累计；`snapshot` 保留为从序列起点累计的可选锚点。
- **默认周期**：EMA、SMA 出厂默认周期由 20 改为 9，后端 `DEFAULT_PERIODS` 与前端 `INDICATOR_DEFAULTS` 一一对应。用户覆盖值只影响新建指标/新建预警，不改变已保存预警与已打开图表的实例参数。
- **预警参数显式化**。主指标和对比指标都固定生效周期、VWAP 锚点、BOLL 倍数和 EMA/ATR 种子版本。新建 EMA/ATR 预警固定为 `pine`。迁移保留旧 EMA/ATR 预警的 `legacy_sma` 语义，并为缺值的历史 BOLL 与 VWAP 预警分别补齐 `2` 和 `snapshot`。
- **Session VWAP 消费方**。预警窗口保留 UTC 会话及其前一根柱，实盘从闭合历史补齐新订阅的起始窗口。关键位复用算子的 UTC 日界和 `FEED_GAP` 重预热规则。
