# 03 — HTTP 按时间范围取 K 线（含冷库之外的最新部分）

**What to build:** 任何使用方（SDK、DSH 工具、前端）都能通过一个 HTTP 接口按标的、周期、时间范围和 `as_of` 取 K 线；返回包括冷库尚未写入、仍在热缓冲中的最新几根，并能区分已收盘和进行中的柱。

**Blocked by:** 01 — 冷库数值格式迁移到按列精度的 decimal128。

**Source of truth:** ADR-0016、ADR-0018；`docs/architecture.md` §4.3。

**Status:** resolved

- [ ] `GET /api/bars?instrument=&timeframe=&start=&end=&as_of=&include_open=` 返回冷库与热缓冲拼接、按 `(ts_event, revision)` 去重的序列；数值为十进制字符串，每根带 `confirmed` 与 `revision`。
- [ ] 热缓冲保留 `confirmed` 与 `revision`，不再按 `ts_event` 覆盖丢失这两个字段。
- [ ] 默认只返回已收盘的柱：收盘时刻已过但确认消息未到的柱不返回；`include_open=true` 时附带进行中的柱，供图表使用。
- [ ] `as_of` 与 01 用同一实现（先截断、再取最大修订）；省略时返回当前可见全部。
- [ ] 契约从 `foundation` 导出 JSON Schema 与 TypeScript 类型，契约零漂移检查通过；OpenAPI 快照同步更新。
- [ ] 测试：跨冷热边界的范围、只在热缓冲中的范围、`as_of` 截断、收盘时刻已过但确认晚到的柱、`include_open` 两种取值。

## Comments

### 实现记录（2026-11-22）

- 新增 `GET /api/bars`。`start`、`end` 和 `as_of` 使用 UTC Unix 毫秒，两个范围边界均包含，省略 `end` 时读取至末尾。响应含标的、周期及按事件时间升序的 `bars`，OHLCV 和 `turnover` 为十进制字符串。默认只返回已确认柱，开启 `include_open` 后可读取热缓冲中当时已收到的进行中版本。
- 热缓冲按事件时间和修订保留确认状态及完整版本。范围查询先捕获热端再读冷库清单，识别已落盘的同一发布，并用冷库共用的选择器先按 `ts_available` 截断，再对每个事件时间取最大可见修订。任务单的 `(ts_event, revision)` 用于识别重复发布，最终每个事件时间返回一根，符合 ADR-0016、ADR-0018 和架构 §4.3。实时写入沿用热端首次确认的 5 秒宽限，重启后对于已有冷库版本的变更按接收时刻续接修订号。
- 范围缺历史时沿用 02 的 REST 只补缺写入器，当前请求在回填完成后重新读取。回填上界受当前时刻和 `as_of` 限制，跳过已存柱及当前热缓冲的时间槽。无离线夹具的回放模式只读取既有冷库及回放中的热端。同步生成了请求和响应 JSON Schema、TypeScript 类型及 OpenAPI 快照。

#### 实际验证

pytest 命令统一使用前缀 `PYTHONPATH=$PWD/core/src /Users/dikui/Project/Talaria/.venv/bin/python -m pytest` 和后缀 `-q -p no:cacheprovider`。

| 中间参数 | 结果 |
| --- | --- |
| `tests/workbench_api/test_bars_api.py` | 首轮 0 通过、1 失败（路由缺失），第二轮 1 通过、2 失败（进行中时点、重启续接） |
| `tests/workbench_api/test_bars_api.py tests/perception/test_hot_snapshot.py` | 9 通过、1 失败（旧快照的热端时点处理，随后修复） |
| `tests/workbench_api/test_bars_api.py tests/perception/test_hot_snapshot.py tests/perception/test_cold_publisher.py tests/feed/test_supervisor.py tests/workbench_api/test_runtime.py tests/workbench_api/test_feed_ws.py tests/foundation/test_feed_contracts.py` | 58 通过、0 失败 |
| `tests/workbench_api/test_bars_api.py::test_range_backfills_missing_history_without_confirming_a_live_preview` | 实现前 0 通过、1 失败，实现后 1 通过、0 失败 |
| `tests/workbench_api/test_bars_api.py::test_exchange_close_replaces_a_synthetic_bar_at_the_grace_cutoff` | 修复前 0 通过、1 失败，最终回归通过 |
| `tests/workbench_api/test_bars_api.py tests/workbench_api/test_openapi.py tests/workbench_api/test_app.py tests/workbench_api/test_runtime.py tests/workbench_api/test_runtime_features.py tests/workbench_api/test_feed_ws.py tests/workbench_api/test_watchlist.py tests/feed/test_supervisor.py tests/replay/test_replay_feed.py tests/perception/test_hot_snapshot.py tests/perception/test_cold_publisher.py tests/perception/test_cold_store.py tests/perception/test_cold_store_decimal128.py tests/perception/test_cold_migrate.py tests/perception/test_raw_store.py tests/foundation/test_feed_contracts.py tests/foundation/test_contracts.py tests/foundation/test_import_boundaries.py tests/cli/test_contract_check.py` | 230 通过、0 失败 |

- `PYTHONPATH=$PWD/core/src /Users/dikui/Project/Talaria/.venv/bin/talaria contract-check` 零漂移。TypeScript `tsc --noEmit -p dsh/packages/talaria-contracts/tsconfig.json` 和 `git diff --check` 通过。

#### 未验证项与风险

- 使用进程内 HTTP、离线回放和替代 REST 数据验证。真实交易所、生产负载下的并发读写与接口吞吐未实测。若重启后同一根柱有多条待落盘修订、其中较早的修订写盘失败，冷库只从已落盘的修订续接，较晚修订在热端和落盘后的编号可能不同。
- `git add -- .scratch/data-access/issues/03-http-range-bars.md` 因沙箱禁止在主仓库的 worktree Git 元数据目录创建 `index.lock` 返回 128。改动保留在 `ticket/data-access-03` 工作区，HEAD 为 `2314a81`。

### 审查修复（2026-11-22）

- 范围查询与实时写盘共用修订分配规则。相同发布采用冷库的修订号，新内容续接编号，较早写盘失败的版本保留已分配的位次。查询覆盖冷读期间的实时发布，范围响应中的十进制字符串在落盘前后保持一致。
- WS 正式确认按自身接收时刻判定。首个已收盘版本在收盘后 5 秒宽限内仍以收盘时刻为 `ts_available`。合成柱只在 `include_open=true` 且该时点无真实柱时作为 `confirmed=false` 的后备值。快照沿用 `main` 的热端修订可见时间，121.5 秒冻结仍选中 121 秒合成的价格 100，122 秒才看到真实修订 101。
- 范围回填在写入前重查热缓冲，保护活跃通道的预览及已确认柱，取消订阅后允许 REST 补齐只有旧预览的时间槽。单次上限 10,000 根，省略 `end` 时以服务当前时间为上界。范围内按实际需要分页，超限返回 400 并提示分段。
- 冷库读取在线程池运行，路由异步等待回填，30 秒超时返回可重试的 504。同一范围的后台回填可以由多个请求共享，进程内并发测试验证等待期间 `/api/health` 可响应。已更新 JSON Schema 和 OpenAPI 快照，重新生成的 TypeScript 类型内容一致。

先运行回归测试复现八项问题，再修复并验证通过。指定 pytest 范围共 612 项通过，7 项因沙箱拒绝绑定 `127.0.0.1` 失败，涉及工作台宿主代理和 Bybit WebSocket 测试。排除这 7 项后的复跑、`contract-check`、TypeScript 类型检查与 `git diff --check` 均通过。快照回归也用 `main` 的 supervisor 和热缓冲实现验证通过。真实交易所与生产负载未实测，改动保留在 `ticket/data-access-03` 工作区。
