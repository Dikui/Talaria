# 08 — 衍生品进入冷库与实时订阅

**What to build:** 标记价、资金费率、持仓量像 K 线一样进入冷库（`标的/类别/月`），实时来自 `tickers.{symbol}`，历史经 REST 回填；资金费率在结算点生成结算事件。SDK、`ctx.derivatives(kind)` 和策略订阅都能取到，没有服务时回测也能用。

**Blocked by:** 02 — 冷库持续写入：收盘柱与历史回填。

**Source of truth:** `docs/architecture.md` §4.2、§4.3；ADR-0017（费率类 scale 固定 8）、ADR-0018。

**Status:** ready-for-agent

- [ ] 冷库写入器支持衍生品分区与清单，数值格式与 01 一致；修订与 `as_of` 规则与 K 线相同。
- [ ] 实时：从现有 `tickers.{symbol}` 读数写入；历史：REST 回填，重复执行幂等。现有快照里的衍生品文件改为从冷库取。
- [ ] 资金费率结算事件（结算时刻、费率、可见时刻）可按范围读取，供成交插件生成 `FundingSettled`。
- [ ] `sdk.derivatives(instrument, kind, start, end, as_of)`；策略订阅可订阅衍生品更新。
- [ ] 测试：仓库样本资金费率 `0.00005` 无损往返；停服务后回测能读到标记价与结算事件；`as_of` 截断。

## Comments

### 实现记录

- 标记价、资金费率、持仓量按 `标的/类别/月` 写入同一份冷库清单。复用原子发布、合并、哈希校验和保留机制。数值分别采用价格精度、固定 8 位和数量精度的 `decimal128(18, s)`，Parquet 物理类型是 `INT64`。合并保留修订，读时先用 `as_of` 截断，再选最高可见修订。
- ticker 读数按秒合批写入，实时首版用事件和接收时间中的较晚值作为可获取时间。订阅启动与快照请求可发起 REST 历史回填。写入线程在发布前复查清单，只补缺。已有快照继续从已登记的附属文件读回，新快照从冷库选版后物化原有的按周期历史序列。
- ticker 公布的下一结算时刻到达后，通过 REST 确认实际结算费率，发布结算事件并记录当时的可获取时间。常规历史回填保留交易所结算时点。`sdk.funding_settlements` 可向成交插件提供这两类已确认结算。内置组件指标沿用快照的结算序列，`ctx.derivatives("funding_rate")` 同时可以查看 ticker 费率。
- `sdk.derivatives` 和默认的 `ctx.derivatives` 直读同一份冷库。`sdk.subscribe_derivatives` 使用现有策略通道的配额、序号和断线重播。游标过期时从同一份本地清单补发修订。
- 未发现任务单与指定 ADR 或架构章节冲突。采用现有标记价与持仓量的 REST 周期语义，并在冷库保留无周期的 ticker 读数。

#### 验证

所有 pytest 命令均为 `PYTHONPATH=$PWD/core/src /Users/dikui/Project/Talaria/.venv/bin/python -m pytest <参数> -q -p no:cacheprovider`。计数为通过数 / 失败数。

| 参数 | 各次执行结果 |
| --- | --- |
| `tests/sdk/test_derivatives.py` | 0 / 1 → 1 / 0，新增 ticker 结算场景后 1 / 1 → 2 / 0 |
| `tests/workbench_api/test_derivatives_api.py::test_bundle_contains_three_series_with_decimal_strings_and_summary` | 0 / 1，快照回填尚未接线 |
| `tests/perception/test_derivative_snapshot.py tests/workbench_api/test_derivatives_api.py` | 10 / 0 |
| `tests/workbench_api/test_strategy_subscription.py::test_sdk_derivative_subscription_recovers_archived_revision_after_service_restart` | 0 / 1 → 1 / 0 |
| 回归命令一 | 264 / 1，OpenAPI 基准缺少新增的 `revision` |
| `tests/components/test_history.py::test_history_net_odds_does_not_treat_a_ticker_rate_as_a_settlement` | 0 / 1，已复现预估费率被计入结算成本 |
| `tests/sdk/test_derivatives.py tests/components/test_history.py::test_history_net_odds_does_not_treat_a_ticker_rate_as_a_settlement tests/workbench_api/test_openapi.py` | 6 / 0 |
| 回归命令二 | 396 / 1 → 397 / 0，修复执行器提交异常导致快照失败 |
| `tests/packs/test_bybit_stream.py::test_tickers_merge_deltas_per_instrument_and_skip_empty_readings` | 1 / 0 |
| `tests/workbench_api/test_app.py::test_failed_cold_history_submission_does_not_fail_chart_or_block_retry` | 1 / 0 |

回归命令一：

```sh
PYTHONPATH=$PWD/core/src /Users/dikui/Project/Talaria/.venv/bin/python -m pytest tests/sdk tests/components tests/perception/test_cold_store.py tests/perception/test_cold_store_decimal128.py tests/perception/test_cold_migrate.py tests/perception/test_cold_publisher.py tests/perception/test_derivative_snapshot.py tests/perception/test_snapshot_roundtrip.py tests/perception/test_hot_snapshot.py tests/workbench_api/test_derivatives_api.py tests/workbench_api/test_strategy_subscription.py tests/workbench_api/test_bars_api.py tests/workbench_api/test_runtime.py tests/workbench_api/test_runtime_features.py tests/workbench_api/test_openapi.py tests/foundation/test_import_boundaries.py tests/foundation/test_contracts.py tests/cli/test_contract_check.py -q -p no:cacheprovider
```

回归命令二：

```sh
PYTHONPATH=$PWD/core/src /Users/dikui/Project/Talaria/.venv/bin/python -m pytest tests/sdk tests/components tests/perception/test_cold_store.py tests/perception/test_cold_store_decimal128.py tests/perception/test_cold_migrate.py tests/perception/test_cold_publisher.py tests/perception/test_derivative_snapshot.py tests/perception/test_snapshot_roundtrip.py tests/perception/test_hot_snapshot.py tests/workbench_api/test_derivatives_api.py tests/workbench_api/test_strategy_subscription.py tests/workbench_api/test_bars_api.py tests/workbench_api/test_runtime.py tests/workbench_api/test_runtime_features.py tests/workbench_api/test_openapi.py tests/workbench_api/test_app.py tests/workbench_api/test_feed_ws.py tests/workbench_api/test_alert_service.py tests/workbench_api/test_indicators_api.py tests/workbench_api/test_key_levels_api.py tests/workbench_api/test_key_level_registry.py tests/workbench_api/test_scanner_api.py tests/packs/test_bybit_connector.py tests/packs/test_fixture_connector.py tests/foundation/test_import_boundaries.py tests/foundation/test_contracts.py tests/cli/test_contract_check.py -q -p no:cacheprovider
```

- `pnpm -C dsh --filter @talaria/desk-ui test` 通过 196 项，`pnpm -C dsh typecheck` 与 `pnpm -C dsh build` 通过。构建报告一个大于 500 kB 的前端分块。
- 按指定命令执行 `contract-check --write` 更新生成物，随后 `contract-check` 零漂移。
- `grep -rnE 'def _pages|fetch_mark_price_bars|fetch_funding_settlements|fetch_open_interest' core/src/talaria/perception/snapshot_builder.py` 无匹配，退出码 1。已检查 `git diff main...HEAD` 和工作区差异，`git diff --check` 通过。

#### 未验证项与风险

- 已通过 fixture 和进程内 ASGI 接线验证。沙箱限制外网与 TCP 监听，未实测真实 Bybit 结算更新、回环 WebSocket 或持续生产负载。首次策略订阅的内容基线和过期游标恢复会读取该类别在冷库中的修订，历史积累后的耗时尚未测量。
- `git add -- .scratch/data-access/issues/08-derivatives-cold-store.md` 返回 128，沙箱禁止在主仓库的 worktree Git 元数据目录创建 `index.lock`。改动保留在当前工作区，HEAD 为 `dbe8ee1adfd44cab22da333c3f4b6640a2708669`。

### 审查修复

- 结算按交易标的和结算时刻选版。实时确认以收到实际费率的时刻为准，历史回填以结算时刻为准。同一费率有更早的合法可获取时间时追加修订，费率更正以收到更正的时刻写入。新增 `settled` 标识区分实际结算和 ticker 预估，旧 Parquet 仍可读取。
- 启订回填截止到首个 ticker 之间的结算点进入确认队列。失败按结算点继续重试，每分钟独立对账并分页补齐。乱序 ticker 按字段维护事件时刻，继承字段时应用当前事件的时间上界。
- 衍生品 SDK 省略上界时使用服务时钟，离线使用本地时钟。在线区间缺数据时，服务通过共享历史执行器回填，SDK 等待完成后直读清单。策略重同步也通过服务核对已有结算，交付更正修订。
- `ctx` 按每根柱首次确认的时刻选取衍生品修订，衍生品事件仍对齐到开盘槽。窗口前分别保留最近结算和最近预估。内置及显式 SDK 读取器的资金成本都只使用实际结算。结算更正生效后，`NET_ODDS` 更新该笔费用。
- 冷库写入关闭时，快照通过连接器直接取数。恢复 `main` 的翻页停止条件，保留达到目标时已取回的整页。以 `main`（`3930d8a`）的 BTC fixture 对照，1m、截止 `1793550000000`、`backfill_bars=10` 时，冷库写入开启和关闭都得到 900 条标记价、30 条结算和 196 条持仓量。

#### 验证

- 各审查项先复现再修复。SDK、组件、衍生品冷库及快照、策略与范围接口相关的定向回归共 245 项通过。
- `PYTHONPATH=$PWD/core/src /Users/dikui/Project/Talaria/.venv/bin/python -m pytest tests -q -p no:cacheprovider` 得到 904 项通过、16 项失败、1 项初始化错误、1 项取消选择。M1 盘面验收 `test_m1_solo_acceptance_flow` 等待流动性异常触发超时，分别在当前工作区和 `5c05aa1` 源码上单独复现。
- `contract-check` 零漂移，`git diff --check` 通过。用 `5c05aa1` 写出的旧冷库和快照 Parquet 验证了结算读取及合并。改动留在 `ticket/data-access-08` 工作区，HEAD 为 `5c05aa1`。
- 真实 Bybit 连线未验证。另有 1 项 DSH 测试因沙箱禁止写用户配置目录的 `cordis.yml` 失败（`test_real_dump_config_matches_permission_snapshot`）。

沙箱禁止绑定回环端口的 15 个用例如下，其中 `test_usage_scan_writes_through_running_service` 在 fixture 初始化时出错。

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
