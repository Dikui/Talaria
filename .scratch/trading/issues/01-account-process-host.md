# 01 — 账户进程宿主：交易方案配置、插槽与回测

**What to build:** `talaria account run scheme.yaml --mode backtest` 启动一个账户进程，按方案装配插件，用 Parquet + 虚拟时钟回放历史，跑一个示例 Python 策略，结果写入账本并可复现。插槽之间的交接契约在本单定好，默认插件在 02、03 实现。

**Blocked by:** analysis/02 — 组件接口、运行时上下文与 Python 写法。

**Source of truth:** ADR-0015；`docs/architecture.md` §6.1、§6.5；GLOSSARY 的 Account Process、Trading Scheme、Plugin Slot。

**Status:** resolved

- [ ] 交易方案配置的 Schema 与校验；插槽注册表；插件声明兼容的接口版本，不兼容时启动失败并说明原因。工具插槽只定接口，不实现插件。
- [ ] 事件循环按“数据 → 策略 → 定仓与风控 → 成交 → 记账”推进，回测与实时同一时钟语义。
- [ ] 插槽交接契约：策略输出意图（不含数量）；定仓输出 `SizingDecision`（数量与依据）；风控只批准或拒绝该数量；`MarginValuation` 只读估值契约定义接口（实现在 02）。
- [ ] 底线 2 在宿主层强制：开仓、加仓、放宽止损只能经风控插槽；只减风险的操作可直达成交插槽。用测试插件证明绕过风控的开仓被拒绝。
- [ ] 复式账本由账户进程单写；回测写入 `runs/<run_id>/`。模拟盘的子账户排他锁（`accounts/<sub_account_id>/owner.lock`）在宿主实现：两个进程声明同一子账户时第二个启动失败。
- [ ] 本任务用最简的测试用定仓、风控、成交插件跑通。
- [ ] 测试：同一方案、数据和种子下两次回测，规范化后的执行事件与复式分录一致（剔除 `run_id`、墙钟时间与数据库物理布局）；两个进程实际争用同一子账户锁。

## Comments

### 实现记录

- `talaria account run scheme.yaml --mode backtest` 加载交易方案 Schema，检查重复键、子账户 ID、插槽归属及接口版本。内置入口 `parquet_replay` 要求明确的标的、周期、UTC 毫秒 `start` 和 `end`，可指定 `history_start`。插槽支持注册名和相对方案目录的 `文件.py:导出名`。策略的 `python:` 写法使用文件导出的 `plugin`。工具插槽提供协议，配置接受空列表。示例方案、组件策略和测试插件见 `tests/account/fixtures/`。
- 回测复用 `sdk.bars(offline=True)` 和组件 `run_history` 的首次确认时点及 `as_of` 截断。宿主用数据入口提供的 UTC 毫秒 `as_of` 推进统一时钟，并把方案种子传给插件。示例按迟到确认时点成交，较晚的修订不改变首次决策。可复现性测试以相同的冷库内容和生效的指标默认值为前提。
- 策略输出经宿主转换为不含数量的 `EntryCandidate`，自动策略归因为 `AGENT`。按 ADR-0015 **拒绝**带 `SetupInsight.max_position` 的意图，并记录 `STRATEGY_QUANTITY_NOT_ALLOWED`。这处理了 analysis/02 组件契约内保留的数量字段。收到 `ControllerPatch` 时记录 `CONTROLLER_PATCH_UNSUPPORTED`。没有发现本单与指定 ADR 或架构章节的直接冲突。
- 风控仅可返回原候选与原 `SizingDecision` 的审批或拒绝。宿主验证开仓和加仓成交引用的审批、回合和累计数量。止损收紧直达成交，放宽须通过风控。减仓、平仓、撤未成交入场单，以及切到更保守的交易状态有直接路由。`MarginValuation` 提供只读逐仓强平与全仓保证金接口，实现归 trading/02。
- 每次回测创建 `runs/<run_id>/ledger.sqlite`。执行事件、分录及分录行在同一事务中追加，按币种精确校验借贷平衡，并可只读比较规范化结果。背靠背两次回测验证了事件与分录一致。模拟盘的启动接口在装配期间及运行期持有全部子账户的 `accounts/<sub_account_id>/owner.lock`，跨进程争用失败会释放已取的其他锁。
- 实时数据适配和模拟盘接线留给 data-access/06、trading/04。模拟盘的 Python 文件加载等待 trading/06 的审批与隔离。真实实时数据、交易所、长区间回放性能及 PyYAML 的干净安装未验证。测试从本地已有的 PyYAML 6.0.3 取得纯 Python 模块，临时加入 `PYTHONPATH`。独立验证了锁文件一致性。本单无删除类验收项。

#### 实际验证

下表各项命令由相同前缀和列出的参数组成。除首轮 Schema 红测使用 `PYTHONPATH=$PWD/core/src`，其余使用以下前缀。

```sh
PYTHONPATH=$PWD/core/src:/private/tmp/talaria-trading01-deps /Users/dikui/Project/Talaria/.venv/bin/python -m pytest
```

| pytest 参数 | 结果（通过 / 失败） |
| --- | --- |
| `tests/account/test_scheme.py -q -p no:cacheprovider --tb=short` | 0 / 7，接口缺失。实现后 7 / 0 |
| `tests/account/test_backtest.py -q -p no:cacheprovider --tb=short` | 0 / 1，CLI 入口缺失。实现后 5 / 0 |
| `tests/account/test_backtest.py -q -p no:cacheprovider --tb=no` | 0 / 5，新增回测与风控场景在宿主接线前失败 |
| `tests/account/test_ownership.py -q -p no:cacheprovider --tb=short` | 0 / 1，宿主启动入口缺失。实现后 1 / 0 |
| `tests/account/test_execution.py tests/account/test_ledger.py tests/account/test_scheme.py -q -p no:cacheprovider --tb=short` | 12 / 2，布尔接口版本和重复待开仓回合被接受。修复后 14 / 0 |
| `tests/account/test_ledger.py -q -p no:cacheprovider --tb=short` | 2 / 1，已有回测账本可重开写入。修复后 3 / 0 |

回归使用以下命令。

```sh
PYTHONPATH=$PWD/core/src:/private/tmp/talaria-trading01-deps /Users/dikui/Project/Talaria/.venv/bin/python -m pytest tests/account tests/components tests/sdk tests/foundation/test_import_boundaries.py tests/cli/test_cli.py tests/cli/test_contract_check.py -q -p no:cacheprovider --tb=short
```

- 146 通过、2 失败。两项均在 `sock.bind(("127.0.0.1", 0))` 时被沙箱以 `PermissionError` 拒绝。失败项为 `tests/cli/test_cli.py::test_up_dual_keeps_fast_path_when_dsh_fails`、`tests/cli/test_cli.py::test_up_dual_keeps_fast_path_when_dsh_binary_missing`。
- 加上 `-k 'not up_dual_keeps_fast_path_when_dsh_fails and not up_dual_keeps_fast_path_when_dsh_binary_missing'` 复跑，147 通过、0 失败、2 项取消选择。两轮均有一条既有 Starlette 弃用警告。未运行全量测试。
- `contract-check` 零漂移，`python -m talaria account run --help` 正常。`uv --no-cache --offline --no-python-downloads lock --check --python /Users/dikui/Project/Talaria/.venv/bin/python` 通过。

已复核 `git diff main...HEAD`、工作区修改及新增文件。逐路径暂存时，Git 退出码为 128，沙箱拒绝创建 worktree 的 `index.lock`。代码、测试及本记录保留在 `ticket/trading-01` 工作区。HEAD 为 `e7ee468d2831de2966459097f1331b0df91f6c64`。预定提交信息为 `feat: 实现账户进程宿主与 Parquet 回测`。

### 审查修复

- 成交回报由宿主校验计划生命周期、回合归属、累计数量、首笔止损、信号柱与决策时点，以及含可选价格滑点的获批入场区间。撤单、计划终态和回合终态使对应审批失效。成交插件回报的入场和平仓都需要逐币种平衡、与成交对价及持仓成本一致的复式分录，全平时结清该回合成本。违规回报拒绝、记录事件并使此次运行报错。
- 每批轮询按发生时间处理。账本提交成功后才更新风控持仓，新意图读取本轮已入账的余额和持仓。直达干预以最近获成交插件接受的止损和数量为基准，数量变化通过成交分录反映到风控持仓。规范化账本将稳定的事件 ID 纳入分录比较。
- 数据入口提供原始读取器，宿主按虚拟时钟构造和截断策略上下文，并把独立的行情副本交给成交插件。策略对同标的同方向且唯一的活跃回合发出的意图沿用该回合止损，并重新经过定仓和风控。模拟盘在运行入口、处理各市场事件与每次写入前核验子账户锁。
- 启动时用插件声明的 Pydantic 模型或组件参数 Schema 严格校验 YAML，错误包含插槽和字段路径。回测先解析交易方案、种子和全部生效的指标默认值，写入 `runs/<run_id>/run-config.json`。给定相同 Parquet 数据和插件代码，`talaria account run <run-config.json> --mode backtest --replay` 使用记录中的配置重放。端到端测试更改了工作台默认值并删除原 YAML，重放账本仍与原运行一致。

#### 实际验证

逐项编写并运行失败复现，再运行修复后的测试。局部回归 120 通过，`contract-check` 零漂移，`git diff --check` 通过。

```sh
PYTHONPATH=$PWD/core/src /Users/dikui/Project/Talaria/.venv/bin/python -m pytest tests/account tests/components tests/sdk tests/cli/test_contract_check.py -q -p no:cacheprovider --tb=short
PYTHONPATH=$PWD/core/src /Users/dikui/Project/Talaria/.venv/bin/python -m pytest tests -q -p no:cacheprovider
PYTHONPATH=$PWD/core/src /Users/dikui/Project/Talaria/.venv/bin/python -m talaria contract-check
```

全量测试 943 通过、15 失败、1 项初始化错误、1 项按默认 online 标记取消选择。另有 1 条既有 Starlette 弃用警告。

沙箱限制本机端口绑定，造成 14 项失败和 1 项初始化错误。

| 测试文件 | 受影响的测试 |
| --- | --- |
| `tests/acceptance/test_m1_solo.py` | `test_m1_solo_cli_loopback_and_token` |
| `tests/acceptance/test_m2_copilot.py` | `test_m2_copilot_dual_plane_scenario` |
| `tests/cli/test_cli.py` | `test_up_dual_keeps_fast_path_when_dsh_fails`、`test_up_dual_keeps_fast_path_when_dsh_binary_missing` |
| `tests/cli/test_dsh_host.py` | `test_host_starts_probes_and_stops_with_redacted_log` |
| `tests/cli/test_graceful_shutdown.py` | `test_sigterm_shuts_down_gracefully` |
| `tests/cli/test_usage_scan_service.py` | `test_usage_scan_fails_closed_when_service_endpoint_is_unreachable`、`test_usage_scan_writes_through_running_service`（初始化错误） |
| `tests/packs/test_bybit_stream.py` | `test_subscribe_ack_then_kline_and_orderbook`、`test_flat_zero_volume_bar_is_no_trades`、`test_rejected_subscription_raises_feed_transport_error`、`test_closed_connection_surfaces_transport_error`、`test_application_ping_and_pong_keep_connection_alive`、`test_tickers_subscription_is_shared_across_timeframes` |
| `tests/workbench_api/test_collaboration_api.py` | `test_host_dispatch_ignores_ambient_http_proxy` |

另有 `tests/cli/test_talaria_desk_profile.py::test_real_dump_config_matches_permission_snapshot` 因 DSH 无权写入沙箱外的 `~/.dsh/profiles/talaria-desk/cordis.yml` 失败。变更保留在 `ticket/trading-01` 工作区，未提交。
