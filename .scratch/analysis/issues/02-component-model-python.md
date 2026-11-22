# 02 — 组件接口、运行时上下文与 Python 写法

**What to build:** 在 Notebook 里（研究模式，不隔离）用 Python 写一个指标或信号组件，通过 `ctx` 取数并在历史区间上运行，得到的结果与图表上同一指标逐值相等；组件试图读取未收盘柱或未收盘的大周期柱时拿不到数据。

**Blocked by:** data-access/04 — Python 取数 SDK。

**Source of truth:** ADR-0019；`docs/architecture.md` §5.1、§5.2；GLOSSARY 的 Component、Runtime Context。

**Status:** ready-for-agent

- [ ] 指标、信号、策略三类组件接口与 `ComponentMeta`（输入、参数及取值范围、输出类型、版本）。
- [ ] `ctx` 复用 data-access/04 的 `as_of` 截断；只在收盘后求值；只暴露已确认的柱（收盘时刻已过但确认未到的柱不可见）；跨周期只暴露最近一根已收盘的大周期柱。
- [ ] 算子注册表：现有 `foundation/operators` 算子注册名称、参数 Schema、版本；注册时必须有黄金向量，缺少时注册失败。
- [ ] 测试：截断式前视审计（任取截断点，截断运行与全区间运行在截断点之前的输出逐值一致）；跨周期非重绘；与图表指标接口结果逐值相等。


## Comments

### 实现记录（2026-11-22）

- `talaria.components` 提供 `ComponentMeta`、`Indicator.compute`、`Signal.evaluate`、`Strategy.on_bar` 和 Notebook 入口 `run_history(component.compute, instrument, timeframe, start=..., end=...)`。参数取值范围用 JSON Schema 表达。ADR-0019 将策略输出概述为“下单意图”，此处采用架构 §5.1 的具体签名 `SetupInsight | ControllerPatch | Abstain`。
- 历史运行以 UTC 毫秒开盘槽下界 `start` 和含边界的收盘截止时刻 `end` 标记区间，可用 `history_start` 提供预热历史。每个计划收盘时刻经 SDK `as_of` 选版，有对应确认柱才求值。若确认在该时刻后到达，该柱会进入后续时刻的输入。大周期柱及指标输出最多返回取数范围内最后一个已确认点。
- 已确认数据的历史可见性沿用 ADR-0018 和已完成的 data-access/03、04。对当前尚未确认的柱，组件读不到数据。首个实时确认若在既有 5 秒宽限内到达，事后按收盘时刻可获取，研究回放无法从 SDK 结果重建这几秒的实际确认延迟。收盘后的其他修订仍按各自可获取时刻选版。
- 九个内置算子均以版本 `1` 注册，声明参数 Schema，并用手算的最小黄金向量逐值验证登记。现有 `contracts/operator_vectors/` 继续覆盖预热、断流和增量。Notebook 与图表共用紧凑柱转换、衍生品对齐及 Decimal 算子。比较图表结果时使用相同取数窗口、截止时刻和生效参数。
- `ctx.derivatives(kind)` 支持传入时点读取器，本单用合成点验证可见性和算子对齐。默认 SDK 衍生品源归 data-access/08。真实交易所、TCP 服务及交互式 Notebook 未实测。

#### 实际验证

pytest 命令统一使用前缀 `PYTHONPATH=$PWD/core/src /Users/dikui/Project/Talaria/.venv/bin/python -m pytest`。表中为其后的完整参数。

| 参数 | 结果 |
| --- | --- |
| `tests/components/test_history.py -q -p no:cacheprovider` | 首轮 0 通过、1 失败（入口缺失），实现后 1 通过。增加截断与图表对照后，1 通过、4 失败（1 个冷库测试批次乱序，3 个指标接口缺失） |
| `tests/components/test_operators.py -q -p no:cacheprovider` | 注册表实现前 0 通过、3 失败，实现后 3 通过 |
| `tests/components/test_history.py -q -p no:cacheprovider --tb=short` | 修正测试批次后 1 通过、4 失败（接口缺失）。实现后 4 通过、1 失败（修订可见时间断言早 1 毫秒） |
| `tests/components/test_operators.py -q -p no:cacheprovider --tb=short` | 3 通过、1 失败（黄金向量验证受 Notebook 的 Decimal 精度影响），修复后随最终回归通过 |
| `tests/components tests/foundation/operators tests/foundation/test_import_boundaries.py tests/sdk tests/perception/test_compaction.py tests/perception/test_snapshot_roundtrip.py tests/perception/test_hot_snapshot.py tests/perception/test_derivative_snapshot.py tests/workbench_api/test_bars_api.py tests/workbench_api/test_indicators_api.py tests/workbench_api/test_indicator_defaults_api.py tests/workbench_api/test_derivatives_api.py -q -p no:cacheprovider` | 两轮分别 201、202 通过，0 失败，均有 1 条 Starlette TestClient 弃用警告 |

`contract-check` 零漂移。截断审计覆盖每个收盘边界和盘中截断点，图表测试使用进程内 HTTP。没有删除类验收项。

已检查 `git diff main...HEAD` 和工作区差异。逐路径暂存任务单时返回 128，沙箱拒绝创建 worktree 的 `index.lock`。改动保留在 `ticket/analysis-02` 工作区，HEAD 为 `5e48db61beb9ff5c91be4bc91e66f6af3d51d590`。
