# 03 — 实时 K 线与盘口流、`NO_TRADES` / `STALE` 断流判定与顶栏健康提示

**What to build:** 看盘页的 K 线随 `Bybit V5 Linear` 实时公共流跳动，进行中 K 线与闭合 K 线、数据修订（`Revision`）正确呈现，顶栏显示最优买卖盘口；行情源在供流窗口内失联时顶栏显示断流状态与持续时长，重连补齐后自动解除。正常稀疏无成交与真实断流被严格区分。

**Blocked by:** 02 — 最窄贯通：Bybit 历史 K 线回填、证据快照冻结与 `--solo` 看盘页显示 K 线

**Source of truth:** [需求文档](../spec.md)、[领域词汇表](../../../GLOSSARY.md)、[系统分层架构规范](../../../docs/architecture.md)、[ADR-0005](../../../docs/adr/0005-fast-path-bypass-and-decoupled-chart-workbench.md)

**Status:** ready-for-agent

- [x] 连接器订阅实时公共流（进行中 / 闭合 K 线、`Revision`、最优盘口），实时事件经统一 `MarketEventFeed` 协议对外提供，供后续预警与扫描复用。
- [x] 供流窗口内无成交产出 `NO_TRADES` 柱（前收盘平盘零量）；传输失联产出 `FEED_GAP` 并把 `Instrument` 置为 `STALE`；重连后回填缺口并恢复。
- [x] 快通道 WebSocket 增量推送 K 线闭合 / 更新、最优盘口与数据源健康状态。
- [x] 离线测试注入确定性实时样本流，覆盖修订追加、`NO_TRADES` 与 `FEED_GAP` 判定、重连补齐。
- [x] 浏览器界面验收（均附截图）：宽度 `1440` 与 `390`、浅色与深色、简体中文下检查实时 K 线跳动、顶栏盘口与断流告警态及恢复后状态。

## Comments

实现摘要（2026-11-19）：

- **L0 契约**：新增 `core/src/talaria/foundation/feed.py`（`KlineEvent` / `OrderBookEvent` / `FeedHealthEvent` / `FeedHealthReport` / `MarketEventFeed` 协议 / `worst_status`），登记 `contracts_io.py` 并重新生成 4 份 JSON Schema 与 `generated.ts`，`uv run talaria contract-check` 零漂移。
- **传输层**：`packs/crypto_perp/stream.py` 接入 Bybit V5 Linear 公共 WS（`wss://stream.bybit.com/v5/public/linear`，topic `kline.{interval}.{symbol}` / `orderbook.1.{symbol}`，订阅 ack、应用层 ping/pong、指数退避重连、优雅关闭，`FeedTransportError`）；`replay/replay.py` 提供 JSONL 回放（`offset_ms`、`disconnect` / `reconnect` 段、可注入 sleep 与时钟）。
- **状态机**：`feed/supervisor.py` 实现 `LIVE / STALE / RECOVERING`、12s 心跳超时、`NO_TRADES`（与历史 `classify_flat_bar` 同口径）与 `FEED_GAP` 合成、断流窗口按事件时间锚定并支持重扫、重连 REST 回填、按 `timestamp` 去重 last-write-wins、`revision` 单调递增、幂等重放。
- **快通道**：`GET /api/feed/health`（fixture 模式 503）、`WS /ws/desk`（`hello` / `bar` / `book` / `health` / `error`、`subscribe` / `unsubscribe`、每客户端 256 条有界队列、慢客户端丢最旧不阻塞广播）；`talaria up --solo --replay-events <path> [--replay-speed F]` 驱动整条链路，`--fixture-dir` 仍只供历史快照，`/api/health.feed_mode` 区分 `fixture` / `replay` / `bybit`。
- **前端**：`features/feed/useMarketStream.ts` 按 `window.location` 推导 WS 地址、指数退避重连、切标的/周期重订阅；顶栏加入最优一档盘口与健康徽标（`LIVE` 实时 / `STALE` 断流红字计时 / `RECOVERING` 恢复中），断流提示条 `data-testid=feed-alert`；K 线增量走 KLineChart v10 `DataLoader.subscribeBar`（同 `timestamp` 更新、新 `timestamp` 追加、`unsubscribeBar` 清理），`FEED_GAP` 不进 `dataList`；状态栏保留 02 的快照信息并加实时推送状态。全简体中文、只使用既有 CSS 变量，未引入 UI 框架。
- **确定性测试**：`tests/fixtures/realtime/*.jsonl`（进行中多次更新→闭合、同 `timestamp` 修订、`NO_TRADES`、断流→`STALE`+`FEED_GAP`、重连+回填→`LIVE`）+ `tests/foundation/test_feed_contracts.py`、`tests/feed/test_supervisor.py`、`tests/replay/test_replay_feed.py`、`tests/packs/test_bybit_stream.py`（本地 fake WS 服务器，无在线依赖）、`tests/workbench_api/test_feed_ws.py`；前端 `features/feed/*.test.ts` 覆盖 reducer 与健康计时纯函数。
- **验收**：`uv run pytest -q` 134 passed；`uv run talaria contract-check` 零漂移；`pnpm -C dsh --filter @talaria/desk-ui test` 26 passed；`pnpm -C dsh typecheck` 与 `pnpm -C dsh build` 通过；`git diff --check` 干净。
- **已知限制**：本机网络被 Bybit CloudFront 403 拒绝，实盘路径只用本地 fake WS 服务器与回放验证，未做在线冒烟；真实盘口仅订阅最优一档（`orderbook.1`），更深的订单流留给后续订单流任务。

Lead review 修复（2026-11-19）：

- **实时投递允许同 `timestamp`**：新增纯函数 `features/market-chart/liveDelivery.ts` 的 `shouldDeliverLiveBar`，`MarketChart` 不再用 `>` 丢弃进行中 K 线的后续修订；同 `timestamp` 低/重复 `revision` 不重复投递，`FEED_GAP` 仍由 `toKLineData` 返回 `null` 跳过。Vitest 覆盖相等投递、更旧不投递、同 `timestamp` 低 `revision` 不重复、高 `revision` 投递。
- **健康契约按通道区分**：`FeedHealthEvent` 新增 `timeframe: str`，`FeedSupervisor` 按 `(instrument_id, timeframe)` 填通道；重新生成 contract schema 与 `generated.ts`；前端 `applyHealth` 同时匹配 `(instrument_id, timeframe)`，切换周期时旧周期健康不会覆盖新周期；`WS /ws/desk` 的订阅过滤对 `health` 同样按周期匹配。
- **浏览器断线可见**：`feedBadgeView` / `feedAlertView` 纯函数在 `live !== false && connection !== "open"` 时优先显示“连接断开 · 重连中”与“与本地守护进程的连接已断开，正在重连；图表保留最后一次快照。”，不再被旧的 `LIVE` 健康掩盖；新增 Vitest 覆盖。
- **状态栏断流计数**：实时 `FEED_GAP` 按 `timestamp` 去重后与历史 gaps 相加（回放样本 `断流 5 处 → 9 处`）。
- **盘口价差**：顶栏盘口增加买一/卖一价差（保留 `pricePrecision`）。
- **验收**：`uv run pytest -q` 135 passed；contract-check 零漂移；desk-ui test 34 passed；typecheck / build 通过；`git diff --check` 干净。重截 `01/02/03/04/05/06/07-*-fix.png` 到 `.scratch/tasks/ticket-03/`，其中 `07-1440x900-disconnected-fix.png` 覆盖客户端 WS 断开提示。
