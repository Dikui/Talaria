# 02 — 冷库持续写入：收盘柱与历史回填

**What to build:** 服务运行时，每根 K 线收盘（`confirmed`）后很快就能在冷库里读到它；服务启动前的历史由服务直接回填进冷库，不再需要有人在前端创建快照。尾部小文件定期合并进分区文件，清单原子切换；合并不丢修订，过去时点的查询结果在合并、重启前后不变。

**Blocked by:** 01 — 冷库数值格式迁移到按列精度的 decimal128。

**Source of truth:** ADR-0018（取代 ADR-0011）；`docs/architecture.md` §4.1、§4.2。

**Status:** ready-for-agent

- [ ] 收盘柱写入所在分区（`标的/周期/月`）的尾部文件；首个已收盘版本 `ts_available` 为柱结束时刻，之后的修订 `ts_available` 为服务收到它的时刻（改掉实时解析把 `ts_available` 固定为柱结束时刻的做法）。
- [ ] 历史回填是冷库写入器的独立路径：图表或范围接口请求的区间缺数据、启用实时订阅的标的启动时，服务用 REST 补齐并直接写入冷库。
- [ ] 后台合并尾部文件进分区文件，合并只重排不去重，`data_manifest.sqlite` 短事务切换清单。
- [ ] 被替换的文件至少保留若干代清单且不少于 24 小时；读者读到已删文件时按新清单整次重试一次。
- [ ] 快照冻结不再触发冷库发布；原快照触发的发布路径与只增游标限制被移除（搜索 `publish_snapshot_bars` 等调用无残留），快照功能本身保留。
- [ ] 冷库写入失败只记 warning，行情与图表不受影响。
- [ ] `--replay-events` 回放模式必须显式指定数据目录，且拒绝使用日常数据目录。
- [ ] 测试：JSONL 回放驱动，收盘后可从清单读到该柱；一根柱先后两个修订，合并前后与重启后用两个 `as_of` 查询结果不变；合并过程中并发读取不报错、不读到半写文件；空数据目录下图表取历史后，服务停掉仍能从冷库读到同样的值；回放前后日常目录不变。

## Comments

### 实现记录（2026-11-22）

- 常驻服务在 `cold_store` 开启时将已收盘交易所 K 线和断线恢复取得的收盘柱提交给同一个后台写入器。按标的、周期和 UTC 月分区写尾部文件，首次收盘版本的可获取时间为柱结束，后续内容变化的修订以接收时间为准。冷库从清单中的最新已发布版本接续修订号。模拟时钟早于收盘或倒退时，可获取时间保持非递减。快照冻结保留原有快照物化和时点选版，移除了快照发布调用和只增游标限制。旧游标元数据继续供 decimal128 迁移工具使用。
- 图表请求现有快照柱分页时，按请求的区间独立检查冷库并经 REST 补缺。首次实时订阅按现有回填页数和目标根数补历史。REST 与磁盘写入在不同工作线程执行。冷库初始化、写入和回填异常记 warning。任务单 03 的独立范围接口可复用本次提供的区间回填入口。回放订阅由 JSONL 驱动，不触发订阅时的 REST 回填。
- 后台默认每 15 分钟合并尾部。合并保留所有行及原时点，使用来源归档哈希记录合并输入，用短事务切换清单。被替换的 Parquet 经过至少 24 小时且产生 3 代更新清单后才回收。离线读者遇到文件消失时，重新读取清单并重做整次查询一次。既有布局及旧规格文件仍由清单读取，合并使用同月同规格的文件。
- 回放目录复用 `TALARIA_DATA_DIR` 或默认 `./.talaria` 的解析规则，并尝试读取默认标签 `com.talaria.workbench` 的当前 launchd 作业和用户 LaunchAgents plist。相对作业路径基于其工作目录解析，显式回放目录与这些目录均通过 `expanduser` 和 `resolve` 规范化。拒绝相同目录、符号链接别名和任一方向的祖先/子目录重叠，检查位于服务配置读取和数据目录锁获取之前。
- 未发现任务单与指定 ADR 的语义冲突。架构 §4.2 的“快照触发、字符串数值”是改造前状态，实施遵循其持续写入目标和 ADR-0017 的 decimal128 约束。

#### 实际验证

pytest 命令统一使用前缀 `PYTHONPATH=$PWD/core/src /Users/dikui/Project/Talaria/.venv/bin/python -m pytest` 和后缀 `-q -p no:cacheprovider`。

| 中间参数 | 结果 |
| --- | --- |
| `tests/perception/test_cold_store.py` | 实现前 27 通过、3 失败 |
| `tests/perception/test_cold_store.py tests/perception/test_cold_store_decimal128.py tests/perception/test_cold_migrate.py` | 54 通过、0 失败 |
| `tests/workbench_api/test_runtime.py tests/workbench_api/test_app.py::test_chart_history_is_readable_from_cold_store_after_shutdown` | 实现前 3 通过、2 失败 |
| `tests/workbench_api/test_runtime.py tests/workbench_api/test_app.py::test_chart_history_is_readable_from_cold_store_after_shutdown tests/feed/test_supervisor.py tests/packs/test_bybit_stream.py` | 18 通过、6 因沙箱绑定失败 |
| `tests/cli/test_cli.py -k replay` | 实现前 5 通过、2 失败。实现后 7 通过、0 失败 |
| `tests/perception/test_cold_store.py tests/perception/test_hot_snapshot.py` | 合并归档改为来源哈希前 41 通过、1 失败 |
| `tests/perception/test_cold_store.py tests/perception/test_cold_store_decimal128.py tests/perception/test_cold_migrate.py tests/perception/test_hot_snapshot.py` | 66 通过、0 失败 |
| `tests/perception/test_cold_store.py tests/perception/test_cold_store_decimal128.py tests/perception/test_cold_migrate.py tests/perception/test_hot_snapshot.py tests/perception/test_snapshot_roundtrip.py tests/perception/test_derivative_snapshot.py` | 75 通过、0 失败 |
| `tests/workbench_api/test_runtime.py tests/workbench_api/test_app.py tests/workbench_api/test_runtime_features.py tests/workbench_api/test_feed_ws.py tests/workbench_api/test_watchlist.py tests/feed/test_supervisor.py tests/replay/test_replay_feed.py` | 两次各 82 通过、0 失败 |
| `tests/cli/test_cli.py -k 'replay or up_dual_rejects_same_port or up_rejects_non_loopback_host or up_rejects_invalid_port'` | 11 通过、0 失败 |

`grep -rnE --include='*.py' 'publish_snapshot_bars|_publish_cold|cold_publishers|ColdBarPublisher|append-only monotonic cursor|published_revisions' core/src tests` 无匹配，退出码 1。`git diff --check` 通过。已检查 `git diff main...HEAD` 及工作区差异。

#### 未验证项与限制

- Bybit WebSocket 的 `test_subscribe_ack_then_kline_and_orderbook`、`test_flat_zero_volume_bar_is_no_trades`、`test_rejected_subscription_raises_feed_transport_error`、`test_closed_connection_surfaces_transport_error`、`test_application_ping_and_pong_keep_connection_alive` 和 `test_tickers_subscription_is_shared_across_timeframes` 均因沙箱禁止绑定 `127.0.0.1` 失败，触发 `PermissionError: [Errno 1]`。真实交易所、实际加载的 launchd 作业及月分区生产负载未实测。
- 沙箱拒绝创建主仓库下的 worktree `index.lock`，`git add -- core/src/talaria/cli/main.py` 返回 128。改动保留在 `ticket/data-access-02` 工作区，HEAD 为 `e2d6722`。

### 审查修复（2026-11-22）

- Bybit V5 K 线从 `kline.<interval>.<SYMBOL>` 主题识别标的。WebSocket 读循环在解析原始报文前记录本地接收时刻，并把该时刻传到热端。回归夹具使用官方报文形状，`data` 数组元素省略 `symbol`。
- 热端对首个交易所收盘版本使用 5 秒宽限。接收时刻不晚于柱结束加 5 秒时，`ts_available` 为柱结束，ADR-0018 的首版规则在宽限内仍成立。超过宽限的首版按接收时刻可见。内容变化的修订按接收时刻可见，取该时刻与热端前一版可获取时刻的较晚值。实时冷库写入直接使用热端给出的 `ts_available`。测试覆盖首版写盘失败后在宽限内到达的修订，以及服务重启后超过宽限才到达的修订。热端版本链以本进程已收到的版本为准，宽限内快速重启时仍可能将修订判为首版。
- 图表、启动订阅和断线恢复取得的 REST 历史统一走只补缺的写入路径。单个写入线程在落盘前复查清单，任何已存版本都会使该柱跳过回填。补入的柱以柱结束作为 `ts_available`。因此冷库会忽略 REST 对已存柱的更正，保留已经发布的实时版本。
- 恢复快照构建器的模块级 logger，标记价、资金费率或持仓量接口失败时记录 warning，K 线快照仍可创建和读取。冷库提交失败也只记 warning，实时回调和图表分页继续，失败的历史请求可再次提交。
- 回放目录在规范化后以 `os.path.samefile` 检查已存在目录的身份相等，以 `casefold` 后的路径检查祖先和子目录关系。无法取得目录身份时，以 `casefold` 后的路径比较相等性。launchd 检查范围仍为默认服务标签。
- 热端合成的 `NO_TRADES` 零量柱仅供热端使用。冷库归档由交易所确认的收盘柱，包括交易所推送的零量柱。新增回归测试在 `11f211d` 即通过，生产代码沿用原有的来源区分。

#### 审查修复验证

指定的 `tests/perception tests/workbench_api tests/feed tests/packs tests/cli tests/replay` 测试共 511 项通过，13 项失败，1 项设置错误，默认排除 1 项在线测试。排除以下 14 项沙箱受阻测试后重跑，511 项通过，15 项排除。

| 文件 | 被沙箱限制的用例 |
| --- | --- |
| `tests/packs/test_bybit_stream.py` | `test_subscribe_ack_then_kline_and_orderbook`、`test_flat_zero_volume_bar_is_no_trades`、`test_rejected_subscription_raises_feed_transport_error`、`test_closed_connection_surfaces_transport_error`、`test_application_ping_and_pong_keep_connection_alive`、`test_tickers_subscription_is_shared_across_timeframes` 均无法绑定回环端口 |
| `tests/workbench_api/test_collaboration_api.py` | `test_host_dispatch_ignores_ambient_http_proxy` 无法绑定回环端口 |
| `tests/cli/test_cli.py` | `test_up_dual_keeps_fast_path_when_dsh_fails`、`test_up_dual_keeps_fast_path_when_dsh_binary_missing` 无法绑定回环端口 |
| `tests/cli/test_dsh_host.py`、`tests/cli/test_graceful_shutdown.py` | `test_host_starts_probes_and_stops_with_redacted_log`、`test_sigterm_shuts_down_gracefully` 无法绑定回环端口 |
| `tests/cli/test_usage_scan_service.py` | `test_usage_scan_fails_closed_when_service_endpoint_is_unreachable` 失败，`test_usage_scan_writes_through_running_service` 在设置阶段出错，均无法绑定回环端口 |
| `tests/cli/test_talaria_desk_profile.py` | `test_real_dump_config_matches_permission_snapshot` 无法写入用户目录下的 DSH profile |
