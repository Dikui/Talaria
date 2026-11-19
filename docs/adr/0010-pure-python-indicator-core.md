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
