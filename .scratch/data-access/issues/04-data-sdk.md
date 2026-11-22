# 04 — Python 取数 SDK：一行代码取到与图表同口径的 K 线

**What to build:** 在 Notebook 或脚本里 `sdk.bars(instrument, timeframe, start, end, as_of)` 就能取到 K 线：历史按清单直读 Parquet，最新几根向服务补齐；服务没运行时只用本地数据。返回 `Decimal` 契约对象，`.to_frame()` 给 float DataFrame。

**Blocked by:** 02 — 冷库持续写入：收盘柱与历史回填；03 — HTTP 按时间范围取 K 线。

**Source of truth:** ADR-0016；`docs/architecture.md` §4.3。

**Status:** ready-for-agent

- [ ] SDK 不依赖 `workbench_api` 和 FastAPI，导入边界测试守住。
- [ ] 直读部分只读清单显式列出的文件；补尾部分按 `(ts_event, revision)` 拼接去重；默认只返回已收盘的柱。
- [ ] `as_of` 复用 01 的唯一实现（先截断、再取最大修订），供以后回测数据入口与组件 `ctx` 复用。
- [ ] 读到的文件已被删除时按新清单整次重试一次（ADR-0018）。
- [ ] 服务未运行时不报错，返回本地数据并在结果上标记未补尾。
- [ ] 测试：与 `GET /api/bars` 对同一范围的结果逐值相等；空数据目录下先由服务回填历史，再停服务，SDK 仍读到同样的值；离线模式；`.to_frame()` 的列与类型。

## Comments

### 实现记录（2026-11-22）

- 新增 `from talaria import sdk` 和 `sdk.bars(...)`。事件时间范围两端均包含，接受 UTC 毫秒与 ISO 日期时间，无时区时按 UTC 解释。数据目录依次取 `data_dir`、`TALARIA_DATA_DIR`、当前目录下的 `.talaria`。仅返回已确认的柱。结果支持索引及迭代，值采用 `RangeBar` 的 `Decimal` 契约。显式离线、服务描述缺失或无法连接时读取本地清单，`tail_completed` 为 `False`。
- SDK 和常驻服务共用回环地址校验及描述文件读取逻辑。SDK 调用同一个 `GET /api/bars`，以可选的 `tail_only=true` 获取未落盘的柱及修订。服务照常回填缺失历史，SDK 在请求结束后通过 `ColdReader` 按清单直读。选择器沿用 01 的 `latest_visible_bars`，先应用 `as_of`，再按事件时间选择最高可见修订，避免漏掉与冷库既有柱同时间槽的补尾修订。文件消失时沿用 ADR-0018 的整次重试。已同步 JSON Schema、TypeScript 类型与 OpenAPI 快照。
- `.to_frame()` 返回 pandas DataFrame。OHLCV 与成交额采用 `float64`，事件时间、可获取时间和修订号采用 `int64`，确认状态采用 `bool`，精度证明采用字符串列。有数据与空结果使用相同列和类型。
- 任务单与指定文档未发现语义冲突。为在既有范围接口上分别取得冷库和热端数据，补尾增加了一个可选查询参数。长范围按既有限额分段，请求省略 `end` 时以 SDK 进程的当前时刻为界。

#### 实际验证

- SDK 离线、热端修订和 DataFrame 三轮测试先红后绿。最终受影响的 Python 测试 167 项通过，覆盖清单孤儿文件、清单轮换及损坏、逐值比较、空冷库回填后停服、离线、分段边界、数据类型和导入边界。
- 通过 `contract-check`、`uv lock --check --offline` 与 `git diff --check`。前端测试 193 项通过，`pnpm -C dsh typecheck` 与 `pnpm -C dsh build` 通过。pandas 及依赖从本机缓存装入临时目录，并附到测试的 `PYTHONPATH`。

#### 未验证项与风险

- 在线路径通过进程内 HTTP 和替代 REST 验证。实际 TCP、交易所和生产负载下并发写读未实测。补尾只传输热端数据，服务端协调修订和回填时仍会扫描相关冷库范围，吞吐及并发一致性实测归 data-access/05。
- `git add -- .scratch/data-access/issues/04-data-sdk.md` 返回 128，沙箱禁止在主仓库的 worktree 元数据目录创建 `index.lock`。改动保留在 `ticket/data-access-04` 工作区，HEAD 为 `1df0a58`。

### 审查修复（2026-11-22）

- `/api/bars` 返回用于协调冷热修订的清单代号 `generation`。冷读在同一个 SQLite 读事务中取得代号和文件列表。SDK 要求所有分段响应与直读清单的代号一致。代号变化时整次重试一次，仍不一致时返回本地结果并标记 `tail_completed=False`。回归测试在补尾响应后发布新修订，覆盖「A 旧值、B 新值」的竞态。
- SDK 先核对数据目录锁的常驻服务角色及 PID，再使用描述文件地址。`/api/health` 和成功的 `/api/bars` 响应返回数据目录标识，SDK 逐次核对。坏描述、跨目录响应和传输中断均降级为本地读取，常驻服务明确返回的 HTTP 504 仍传给调用方。
- HTTP、在线 SDK 和离线 SDK 在省略 `end` 时都截到最后一根已收盘柱。在线 SDK 从 `/api/health` 取服务时钟，离线冷读使用本地时钟。ISO 区间先按原始精度校验，再将 `start` 向上、`end` 向下取整到毫秒。有效区间内没有整数毫秒时返回空结果。
- 六项均先复现失败再修复。指定的 `tests/sdk tests/workbench_api tests/perception tests/foundation tests/acceptance` 范围共 623 项通过。`test_host_dispatch_ignores_ambient_http_proxy`、`test_m1_solo_cli_loopback_and_token` 和 `test_m2_copilot_dual_plane_scenario` 因沙箱拒绝绑定 `127.0.0.1` 失败。`test_m1_solo_acceptance_flow` 等待关键位和画线预警超时，基线 `6c1e762` 单独复跑在同一处超时。
- 排除上述四项后复跑为 623 项通过。pytest 沿用临时 pandas 依赖目录。`contract-check` 和 `git diff --check` 通过，CLI 与 OpenAPI 补充检查共 36 项通过。改动保留在 `ticket/data-access-04` 工作区，HEAD 为 `6c1e762`。
