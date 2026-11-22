# 06 — 策略实时订阅与按使用方持有的订阅配额

**What to build:** 策略和脚本用 `async for ev in sdk.subscribe(instrument, timeframe)` 订阅已收盘的 K 线修订。服务端队列满时断开连接而不是丢弃；SDK 重连后按变更序号补齐，不漏断线期间对旧柱的修订，也不漏补齐与重新订阅之间收盘的柱。上游实时订阅配额改为按使用方和 `(标的, 周期)` 持有，没有打开任何窗口的策略和非 `1m` 的预警也能拿到行情。图表页面的“满了丢最旧”保持不变。

**Blocked by:** 04 — Python 取数 SDK。

**Source of truth:** ADR-0016；`docs/architecture.md` §4.1。

**Status:** resolved

- [ ] 新的策略订阅 WS 端点与图表 `WS /ws/desk` 分开，语义不同；交付单位是一个已收盘柱的修订，带变更序号 `(epoch, seq)`。
- [ ] 重连协议：客户端带最后序号；服务先挂实时订阅并缓冲，再从变更日志补发更大序号的修订，最后放出缓冲并按序号去重。序号超出保留范围或 `epoch` 变化时返回需要重新同步，SDK 经范围接口重取并按 `(ts_event, revision)` 去重。
- [ ] 订阅配额按 `(标的, 周期)` 记在使用方名下（图表页、预警按自己的周期、账户进程、自选）；账户进程的订阅优先保留，配额不足时明确拒绝；被挤掉的使用方收到通知。
- [ ] 测试：人为让客户端变慢，服务断开、SDK 补齐后序列无缺口无重复；断线期间旧柱出现新修订；恰好在补齐与重新订阅之间收盘的柱；服务重启后 SDK 自动重新同步；无窗口时策略订阅能拿到行情；单独一条 `5m` 指标预警能收到 `5m` K 线；配额满时账户进程得到明确拒绝。
- [ ] 记录一次实时延迟（事件到回调 p50/p99），与 D2 实测数据对比。

## Comments

### 实现记录

- `sdk.subscribe(instrument, timeframe)` 通过独立的 `WS /ws/strategy` 交付已确认的柱修订。变更序号按服务代、标的和周期递增。每连接的实时队列最多 256 条，溢出以 `1013` 关闭；每通道保留最近 4,096 条变更，异常断线后原使用方保留配额 30 秒。图表继续沿用满时丢最旧的行为。
- 服务在同一个事件循环步内先挂实时队列，再检查游标和拷贝变更日志。日志有效时依序补发，SDK 检查游标连续性。服务代变化、游标落在日志之外或通道被释放后，SDK 在实时队列已挂好的情况下请求 `/api/bars`，以可选的 `available_since` 取全部可见确认修订，再以 `(ts_event, revision)` 去重。范围重取的事件使用 `seq=None`，实时事件和日志补发保留服务序号。原有范围查询继续按时点选择每根柱的最新可见修订，回填只补缺。
- 图表、预警、账户进程和自选以 `(标的, 周期)` 共享上游通道。按 ADR-0016，账户进程可共享已有通道，申请新通道遇到满额时显式拒绝，已经持有的账户通道受保护。图表、预警、自选依次分配剩余通道，降级经工作台 WS 通知。预警按自身周期处理断流与恢复，过期后释放配额。任务单与指定文档未发现直接冲突。

#### 验证

pytest 命令统一采用 `PYTHONPATH=$PWD/core/src /Users/dikui/Project/Talaria/.venv/bin/python -m pytest` 和 `-q -p no:cacheprovider`。

- `tests/workbench_api/test_strategy_subscription.py` 续做前 8 项通过。新增 `test_alert_expiring_during_a_feed_event_releases_its_quota` 和 `test_stale_chart_channel_does_not_suspend_a_healthy_alert_period` 均单独运行，修复前各 1 失败，修复后各 1 通过。
- `tests/workbench_api/test_strategy_subscription.py tests/workbench_api/test_bars_api.py tests/workbench_api/test_feed_ws.py tests/workbench_api/test_watchlist.py tests/workbench_api/test_alerts_api.py tests/workbench_api/test_alert_service.py tests/workbench_api/test_runtime.py tests/workbench_api/test_runtime_features.py tests/workbench_api/test_openapi.py tests/workbench_api/test_app.py tests/perception/test_hot_snapshot.py tests/perception/test_cold_publisher.py tests/perception/test_alerts_engine.py tests/feed/test_supervisor.py tests/sdk tests/foundation/test_import_boundaries.py tests/foundation/test_contracts.py tests/cli/test_contract_check.py` 中间回归 248 项通过，最终回归 249 项通过。无沙箱导致的 pytest 失败。
- `contract-check` 零漂移。`pnpm -C dsh --filter @talaria/desk-ui test` 通过 193 项，`pnpm -C dsh typecheck` 和 `pnpm -C dsh build` 通过。

#### 实时延迟

单客户端，合成的已确认柱，每组预热 20 条、测量 200 条，由 `FeedSupervisor.ingest` 计时至 SDK 迭代器收到事件。三组交付序号均连续。沙箱拒绝绑定 `127.0.0.1`，以进程内 ASGI WS 消息传递验证实际服务路由、序列化及 SDK 解析。D2 使用本机 TCP WebSocket 原型，延迟仅作量级比较。单位为 µs。

| 负载 | 本单 p50/p99 | D2 本机 WS 单客户端 p50/p99 |
| --- | ---: | ---: |
| 10 条/s，关闭冷库写入 | 969 / 1,487 | 1,006 / 1,279 |
| 10 条/s，开启冷库写入 | 3,810 / 4,820 | 1,006 / 1,279 |
| 200 条/s，开启冷库写入 | 671 / 1,538 | 423 / 913 |

#### 未验证项与风险

- 未实测真实交易所、本机 TCP 或生产数据量下的重同步性能。重同步需要检查历史范围中的迟到修订，长时间运行后的读取成本取决于冷库大小。服务重启后可恢复的版本受已有冷库清单约束。关闭冷库写入或写盘失败时，重启可能无法找回只留在上一代内存中的修订。
- `git add -- .scratch/data-access/issues/06-strategy-subscription.md` 返回 128，沙箱禁止在主仓库的 worktree Git 元数据目录创建 `index.lock`。改动保留在 `ticket/data-access-06` 工作区，HEAD 为 `5e48db61beb9ff5c91be4bc91e66f6af3d51d590`。

### 审查修复（2026-11-22）

- 策略订阅、HTTP 范围读取和冷库续编共用 `perception/bar_range.py` 的冷热修订协调代码。实时收盘、REST 恢复和历史回填写盘都会通知订阅按范围重读。服务以当前时钟选出可见的真实确认修订，未来版本到期后重新选版。REST 恢复柱已占用首次收盘的可获取时刻，后续不同值的 WS 确认使用实际接收时刻。冷库修订之后才到达的同值确认与其当时对应的冷库版本对齐。
- 首次握手以通道日志的 `(epoch, seq)` 及该序号处已可见的内容清单界定起点。SDK 分帧接收清单，内容身份包含事件时间、可获取时间、精度和规范化 OHLCV/成交额。同一毫秒反复变回相同数值时，按权威修订顺序区分重复出现的内容。更换服务代后，即使修订号重新从零开始，也可区分重发和真正的变化。
- SDK 在 WS 已挂好实时缓冲后经 `/api/bars?resync_from=…` 重取。该参数界定必须连续覆盖的收盘时间槽。查询还核对请求范围中已知的旧槽。在配置历史来源时，服务重新向交易所取这些槽，串行比较并发布差异。因此最后一份租约到期后，已归档的柱仍能发现更正。常规 HTTP 查询保留原有只补缺行为。
- **重同步按 `ts_event` 升序交付，同一时间槽按权威修订顺序交付。** 再接续挂载时已开始缓冲的实时序号。只有可见确认柱才算覆盖。交易所查询失败、受保护的预览挡住补缺、写入失败或请求超时时，范围接口返回可重试的 503 或 504。SDK 在完整分段全部成功前保留原游标，也检查每段应覆盖的确认槽。
- 每个策略通道合并重复范围通知并限制积压。积压超过日志容量时使游标失效，通知客户端重同步。热缓冲按最近更新保留时间槽，后台读取时固定对应槽，复制版本、冷热协调、内容计算和范围覆盖检查均在线程中完成。最后一个策略释放或租约到期时清除日志、待处理范围、后台任务和可见性计时器。配额检查及申请失败时也清理临时状态。
- 普通预警只在 ARMED 时申请通道，关键位预警在 ARMED 和 COOLDOWN 期间继续维护。配额退订使对应周期的预警暂停求值，恢复 LIVE 后首个有效观察建立基线。工作台连接时获取当前配额不足的预警 ID，后续配额变化更新列表。图表识别带标的和周期的拒绝帧，显示等待状态并自动重订当前通道。

#### 审查修复验证

- 审查反例先单独复现，再经实际 ASGI WS、SDK、HTTP 及冷库验证。覆盖旧柱连续续号、回填与同值确认、服务时钟、跨代同号不同内容及异号同内容、同毫秒回到原值、断线范围修订、部分分段失败后保留游标、租约与排队读清理、预警冷却与重新建立基线。前端 Vitest 复现并验证配额拒绝后的重订、预警断线重连及配额恢复。
- 单柱 20,000 次预览后，同时处理正式确认与 HTTP 范围请求。1 ms 事件循环心跳的最大间隔在关闭及开启冷库写入时分别为 **5.06 ms 和 6.04 ms**，回归门槛 15 ms。原实现仅处理确认时测得约 119 ms。这里测量事件循环停顿。上面的旧版事件到 SDK 回调延迟未重新测量。
- 按要求运行全量 pytest，813 项通过，1 项真实网络测试按项目配置排除。15 项因沙箱禁止绑定回环端口未能完成（其中 1 项在 fixture 设置时出错），另 1 项真实 DSH Profile 测试因默认目录只读失败。把已安装的同一 Profile 复制到临时可写的 `DSH_HOME` 后，该测试单独通过。使用同一临时 Profile 并只排除上述 15 项回环用例，整套复跑为 **814 项通过、16 项排除**（含项目默认排除的 1 项在线测试）。
- `pnpm -C dsh --filter @talaria/desk-ui test` 通过 195 项，`pnpm -C dsh typecheck` 通过，`contract-check` 零漂移。真实交易所、浏览器和生产规模下的历史基线传输、断线后重新核对历史的耗时未实测。首次订阅的内容清单大小取决于通道中已可见的历史修订数。
- 受端口绑定限制的用例如下。

| 测试文件 | 用例 |
| --- | --- |
| `tests/acceptance/test_m1_solo.py` | `test_m1_solo_cli_loopback_and_token` |
| `tests/acceptance/test_m2_copilot.py` | `test_m2_copilot_dual_plane_scenario` |
| `tests/cli/test_cli.py` | `test_up_dual_keeps_fast_path_when_dsh_fails`、`test_up_dual_keeps_fast_path_when_dsh_binary_missing` |
| `tests/cli/test_dsh_host.py` | `test_host_starts_probes_and_stops_with_redacted_log` |
| `tests/cli/test_graceful_shutdown.py` | `test_sigterm_shuts_down_gracefully` |
| `tests/cli/test_usage_scan_service.py` | `test_usage_scan_fails_closed_when_service_endpoint_is_unreachable`、`test_usage_scan_writes_through_running_service`（fixture） |
| `tests/packs/test_bybit_stream.py` | `test_subscribe_ack_then_kline_and_orderbook`、`test_flat_zero_volume_bar_is_no_trades`、`test_rejected_subscription_raises_feed_transport_error`、`test_closed_connection_surfaces_transport_error`、`test_application_ping_and_pong_keep_connection_alive`、`test_tickers_subscription_is_shared_across_timeframes` |
| `tests/workbench_api/test_collaboration_api.py` | `test_host_dispatch_ignores_ambient_http_proxy` |
