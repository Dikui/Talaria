# 08 — 自选观察池（`Watchlist`）与实时订阅优先级

**What to build:** 交易员在看盘页管理 `Watchlist` 分组（增删改、排序），点击标的切换主图；守护进程按优先级分配实时流订阅配额，当前打开图表的 `Instrument` 优先于普通自选。

**Blocked by:** 03 — 实时 K 线与盘口流、`NO_TRADES` / `STALE` 断流判定与顶栏健康提示

**Source of truth:** [需求文档](../spec.md)、[领域词汇表](../../../GLOSSARY.md)、[系统分层架构规范](../../../docs/architecture.md)、[ADR-0005](../../../docs/adr/0005-fast-path-bypass-and-decoupled-chart-workbench.md)

**Status:** ready-for-agent

- [x] 手工分组的增删改查持久化；分组模型支持系统维护的动态分组（`SCANNER_FOCUS` 由 15 写入，本任务只需保证该类分组只读展示）。
- [x] 订阅调度器按可扩展的优先级规则分配配额：当前打开图表 > 普通自选；“挂有活跃预警”一级由 13 接入。配额不足时低优先级标的降级为非实时并在列表中标明。
- [x] 列表展示各标的最新价、涨跌与 `STALE` 状态。
- [x] 浏览器界面验收（均附截图）：宽度 `1440` 与 `390`、浅色与深色、简体中文下检查自选列表与分组；覆盖长分组名与长合约标识不溢出、空 `Watchlist` 引导态。`390` 下可浏览与切换标的，分组编辑可隐藏。


## Comments

2026-11-19 实现完成，分支 `ticket-08-watchlist`（基线 `c4ff005`，本单提交见分支末提交），未合并、未推送。

- 持久化：`state/collaboration_state.sqlite` 新增 `watchlist_groups`（`group_id`、`name`、`position`、`kind`、`is_system`）与 `watchlist_items`（`group_id`、`instrument_id`、`position`，级联删除）。所有读写经 `StateWriterQueue.run` 提交到单一串行协程，沿用 WAL + `synchronous=FULL`，请求协程不直接写 SQLite；系统组位置固定在手工分组之后，`upsert_system_group` 预留给 15 写入，本单只读展示。
- 订阅调度：新增 `RealtimeSubscriptionScheduler`，按“当前图表 > 活跃预警（`alerted` 扩展点，13 接入）> 普通自选”分配配额；图表用引用计数保证同一 `(标的, 周期)` 只订阅一次，WS 退订/关闭后重新 `reconcile`，避免重复订阅与泄漏。图表订阅始终优先，配额不足的普通自选降级并标记 `QUOTA`；无行情能力时标记 `NO_FEED`。未改动 `FeedSupervisor` 公共接口。
- 行情与 STALE：快照/列表优先读取 supervisor 最近 1m 柱（来源 `SUPERVISOR`）；未实时订阅时回退 `connector.fetch_bars(limit=2)`（fixture/最近快照，5 秒缓存，来源 `SNAPSHOT`），不可用为 `UNAVAILABLE`；`STALE` 严格由 `FeedHealthStatus` 判定，不用时间猜测。
- API：`GET /api/watchlist`、分组 CRUD、`PUT /api/watchlist/groups/order`、条目新增/删除、`PUT /api/watchlist/items/order`。写操作统一 Bearer 鉴权；未知分组/标的 404，空名称、重复标的、非法排序 422。`DELETE /api/watchlist/items/{instrument_id}` 按契约从所有手工分组移除，仅存在于系统组时返回“系统分组只读”。
- 前端：新增 `features/watchlist`，`App` 仅接线 `WatchlistPanel`；左栏分组/条目、点击切主图、分组与条目排序、增删改；长分组名/长合约标识 ellipsis + `title`；空态中文引导；SCANNER_FOCUS 只读。`390` 下保留浏览与切换，编辑控件隐藏；浅/深色沿用现有 token。
- 测试：`uv run pytest -q` → `244 passed, 1 deselected`；`uv run talaria contract-check` → `契约生成物零漂移。`；`pnpm -C dsh --filter @talaria/desk-ui test` → `51 passed`；`pnpm -C dsh --filter @talaria/desk-ui typecheck`、`build` 通过（仅 Vite 500 kB chunk 提示）；`git diff --check` 通过。新增后端覆盖 CRUD/重启持久化/排序/系统组只读/配额优先级/图表引用计数/STALE/鉴权；前端纯函数覆盖排序、空态、切换判定、390 只读与行情标签。
- 浏览器验收（ego-browser，fixture 守护进程）：`uv run talaria up --solo --port 8899 --data-dir .scratch/tasks/ticket-08/data --fixture-dir tests/fixtures/bybit --replay-events tests/fixtures/realtime/in_progress.jsonl --replay-speed 1 --realtime-quota 1`，URL `http://127.0.0.1:8899/desk`。
  - `1440-empty-light.png`：1440×900 浅色，空 Watchlist 中文引导“自选观察池还是空的”，配额显示 `实时 1/1`，图表与左栏并排。断言：`.watchlist-empty` 可见、无横向溢出、图表矩形与左栏同一行。
  - `1440-dark-groups.png`：1440×900 深色，2 个分组/4 个标的；长分组名 `scrollWidth 403 > clientWidth 87` 且带 `title`，`1000BONK-USDT-PERP` 标识不溢出；徽标为 `实时 / 配额降级 ×3`（quota=1，图表 BTC 优先），长标识可完整悬停查看。
  - `390-readonly.png`：390×844 浅色；点击 ETH 行后顶栏与选中态均切到 `ETH-USDT-PERP`；所有 `.watchlist-edit` 计算样式为 `display:none`，页面无横向溢出，图表仍可见。
  - 截图未提交（按约束不提交截图/data/dist）；绝对路径见最终报告。
- 已知限制：fixture 仅含 BTC/ETH/1000BONK，SOL 在验收中显示“无报价”；回放脚本结束后 supervisor 靠空心跳保持 LIVE，行情柱不再增长；系统组写入 API 留给 15；1m 报价缓存 5 秒，非逐笔。
