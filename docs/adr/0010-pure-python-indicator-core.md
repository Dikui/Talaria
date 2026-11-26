---
status: accepted（已由 ADR-0023、ADR-0025 修订：Python 为算子与定仓预演黄金向量唯一数学权威，Rust 实现须 100% 逐字符对齐）
---

# 指标核以 Python 黄金向量为唯一数学权威，Rust 与 Python 定点实现严格对齐

> **2026-11 演进修订（见 [ADR-0023](./0023-standalone-workbench-and-rust-data-engine.md)）：** 为支撑链路 1 独立看盘与单个 Rust `Resident Service`（`talaria-data`）零 Python 运行，系统由“只在 Python 实现一份”升级为**以 Python `core/src/talaria/foundation/operators/` 与 `contracts/operator_vectors/` 全部黄金向量（含技术指标、衍生品算子与 `Key Level` 纯函数）作为跨语言唯一数学权威**：
> - Rust `talaria-operators`（`engine/crates/talaria-operators/`）必须基于 `rust_decimal::Decimal` 实现全部算子与 `Key Level` 引擎，在 `NO_TRADES` 平盘递推、`FEED_GAP` 输出 `null` 并重置预热以及摆动点 `confirm_bars` 延迟规则上保持逐字符一致，并通过 `contracts/operator_vectors/` 全部 11 份黄金向量的自动化回归门禁。
> - 前端浏览器及 TypeScript 运行时仍严禁自行计算任何技术指标或衍生品算子，统一消费服务返回的权威结果。

（以下保留原 2026-11 初版决定供历史溯源）

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

## 追加：定仓预演的 Rust 实现同样以 Python 黄金向量为准（2026-11，见 [ADR-0025](./0025-paper-account-projection-and-intent-queues.md)）

为满足工作台图表与表单拖动止损线时 `< 5ms` 的低延迟实时回显（见 [architecture.md](../architecture.md) §6.3），定仓预演（`SizingPreview`）采用与指标算子相同的跨语言对齐原则：

- **Python 为唯一数学权威**：`core/src/talaria/account/plugins/r_unit.py` 与 `risk_math.py` 产出全部定仓预演黄金向量（覆盖 `r_unit` 公式、双边摩擦缓冲 `c_buf`、合约乘数、最小下单量与步长取整、手动指定数量核对、逐仓预估强平价与距离，以及各类拒绝原因）。
- **Rust 逐字符对齐门禁**：`talaria-data` 侧的无副作用定仓预演实现基于 `rust_decimal::Decimal` 与账户投影状态计算，必须 100% 逐字符通过 Python 生成的全部定仓黄金向量回归测试。
- **预演仅用于展示**：真实下单的权威数量仍由模拟盘 `Account Process` 在接收已确认草案时由 Python 定仓插槽计算；前端 TypeScript 严禁自行实现定仓公式。
