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

### 审查修复（2026-11-22）

- 回放在每根基础周期柱的首个确认版本可见时求值。回查较早的可见修订，恢复被迟到修订遮住的首次确认。同一可获取时刻按开盘槽升序求值，基础周期读到触发柱为止。后续求值可读取已经可见的修订。替代读取器返回重复、乱序或未确认的柱时，运行时先过滤，再用 SDK 的规则取每个事件的最高可见确认修订。
- 黄金向量、注册算子、批量和增量计算共用精度 28、`ROUND_HALF_EVEN` 的本地 Decimal context。注册了 `legacy_ema@1`、`legacy_atr@1`，逐点对照旧预警的 `legacy_sma` 路径。迁移时传入预警已保存的周期。
- 大周期自动向预热起点前多读 5,001 个开盘槽，提供最大指标周期及首个变化量所需的历史。`ctx` 只暴露最近的已确认大周期柱及对应指标点。递推指标如需对齐更早的历史，使用与持续运行相同的 `history_start`。
- 衍生品读取包括每个序列在窗口前最新的可见点。共享对齐逻辑在每根柱开盘时按可获取时间过滤，再按事件时间取最新值。迟到的旧结算不会覆盖更新的结算。运行时根据算子的衍生品输入声明取数，按周期对齐持仓量及标记价。未接入衍生品源时内置衍生品指标返回空值，净赔率区分未知资金成本和观测到的零费率。
- `Ctx.tail_completed` 和 `Evaluation.tail_completed` 传播确认查找及求值期间 SDK 的补尾状态。回放开始时只读加载同数据目录的工作台指标默认值，按字段合并显式参数并以显式值为准。Notebook 可用 `data_dir` 指定该目录。
- ADR-0018 的 5 秒宽限是既定口径。宽限内到达的首个确认在回放中按收盘时刻可见，与实时求值时刻最多相差 5 秒，信号内容一致。净赔率每柱计一次结算是既有图表限制。`SetupInsight` 的数量字段归 trading/01。

#### 实际验证

每项先写回归测试再修改实现。红测分别复现迟到确认漏评、Decimal 运算异常、大周期预热为空、窗口前衍生品丢失及旧结算覆盖、净赔率使用未知资金成本、旧算子未注册、误读未声明的衍生品、降级字段缺失、重复修订及工作台默认值不一致。对应回归均已通过。

- `PYTHONPATH=$PWD/core/src /Users/dikui/Project/Talaria/.venv/bin/python -m pytest tests -q -p no:cacheprovider` 实际为 818 通过、15 失败、1 项初始化错误、1 项按仓库配置取消选择。复跑 16 个未通过用例，均再次触发同一沙箱限制。
- `contract-check` 退出码 0，契约生成物零漂移，对应 pytest 文件 4 项通过。`git diff --check` 通过。改动留在工作区。

以下 15 个用例被沙箱禁止绑定回环端口，含一个用例的 fixture 初始化错误。

```text
tests/acceptance/test_m1_solo.py::test_m1_solo_cli_loopback_and_token
tests/acceptance/test_m2_copilot.py::test_m2_copilot_dual_plane_scenario
tests/cli/test_cli.py::test_up_dual_keeps_fast_path_when_dsh_fails
tests/cli/test_cli.py::test_up_dual_keeps_fast_path_when_dsh_binary_missing
tests/cli/test_dsh_host.py::test_host_starts_probes_and_stops_with_redacted_log
tests/cli/test_graceful_shutdown.py::test_sigterm_shuts_down_gracefully
tests/cli/test_usage_scan_service.py::test_usage_scan_fails_closed_when_service_endpoint_is_unreachable
tests/cli/test_usage_scan_service.py::test_usage_scan_writes_through_running_service
tests/packs/test_bybit_stream.py::test_subscribe_ack_then_kline_and_orderbook
tests/packs/test_bybit_stream.py::test_flat_zero_volume_bar_is_no_trades
tests/packs/test_bybit_stream.py::test_rejected_subscription_raises_feed_transport_error
tests/packs/test_bybit_stream.py::test_closed_connection_surfaces_transport_error
tests/packs/test_bybit_stream.py::test_application_ping_and_pong_keep_connection_alive
tests/packs/test_bybit_stream.py::test_tickers_subscription_is_shared_across_timeframes
tests/workbench_api/test_collaboration_api.py::test_host_dispatch_ignores_ambient_http_proxy
```

`tests/cli/test_talaria_desk_profile.py::test_real_dump_config_matches_permission_snapshot` 触发另一项沙箱限制，DSH 无权写入用户配置目录下的 `cordis.yml`。
