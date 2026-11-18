# Talaria

Talaria 是交易员的本地分析工作台：Python 底座负责历史行情回填、快照冻结、实时行情编排与契约，React 前端负责 K 线、盘口与断流健康展示。当前版本对应任务单 03：在 02 的只能看历史快照的只读看盘之上，接入 Bybit 公共实时流、断流健康判定与本地回放。

## 环境

- Python 3.14+，[uv](https://docs.astral.sh/uv/)
- Node.js 26+，pnpm 11+
- 前端图表锁定 `klinecharts@10.1.1`

## 快速启动（离线确定性样本）

```bash
uv sync
pnpm -C dsh install
pnpm -C dsh build

# 终端 1：只绑定 127.0.0.1:8765，使用仓库内的确定性 Bybit fixture
uv run talaria up --solo --fixture-dir tests/fixtures/bybit
```

浏览器打开 <http://127.0.0.1:8765/desk>。`/desk` 会把随机会话 token 写入同源的 `talaria_token` cookie；前端读取后放入 `Authorization: Bearer`，所有写操作都必须带这个头。

`--fixture-dir` 只提供历史快照：没有实时行情时 `/api/feed/health` 返回 503，顶栏显示“离线样本”。要看到实时跳动与断流健康状态，使用本地回放（可同时给 `--fixture-dir` 供历史快照）：

默认数据目录是 `./.talaria`，可用 `--data-dir` 或 `TALARIA_DATA_DIR` 覆盖。目录结构：

```text
<data-dir>/data/raw/bybit_linear/<SYMBOL>/<timeframe>.jsonl   # 追加、去重、fsync
<data-dir>/state/runtime_snapshots.sqlite                     # WAL 快照清单
<data-dir>/state/hot_snapshots/<snapshot_id>.parquet          # 一次性原子写出
```

## 常用命令

```bash
# 后端测试（在线冒烟默认跳过）
uv run pytest

# 契约零漂移检查；--write 用于重新生成
uv run talaria contract-check
uv run talaria contract-check --write

# 前端测试、类型检查与构建
pnpm -C dsh --filter @talaria/desk-ui test
pnpm -C dsh typecheck
pnpm -C dsh build
```

重新生成确定性 fixture（固定种子，BTCUSDT 1m 900 根原始柱（紧凑序列 905 根，含 5 根缺口）与 1 根零量平盘）：

```bash
uv run python tests/fixtures/bybit/generate.py
```

## 真实 Bybit（可选）

本机所在网络可能被 CloudFront 按国家/地区拒绝（HTTP 403），因此**真实在线回填不是本任务单的验收前提**。在可访问 Bybit 的沙箱外可显式运行：

```bash
uv run pytest -m online tests/packs/test_online_bybit.py
uv run talaria up --solo --bybit-base-url https://api.bybit.com
```

`talaria up` 不带 `--solo` 会提示“DSH 协同模式将在 M2 提供，请使用 --solo”并退出。`--solo` 只接受回环 IP；端口默认 8765。

## 实时流与本地回放

实时流的唯一入口是 `MarketEventFeed` 协议（K 线事件、最优一档盘口事件、健康事件），状态机位于 `core/src/talaria/feed/supervisor.py`：

| 状态 | 含义 | 进入条件 | 退出条件 |
| --- | --- | --- | --- |
| `LIVE` | 交易时段内持续供流 | 订阅成功或数据恢复 | 12s 内没有任何事件（含心跳） |
| `STALE` | 断流挂起，禁止基于旧价决策 | 超过 `stale_after_ms`（默认 12000）无活动，或传输层报错 | 传输层重连成功 |
| `RECOVERING` | 正在重连并回填 | `transport_restored` 收到 REST 回填 | 回填应用完毕 |

状态机把“每一分钟都必须有 K 线”的交易所语义落实为两类合成柱：

- `NO_TRADES`：没有断流，只是该分钟确实没有成交——以前一根收盘价生成平盘零量柱；
- `FEED_GAP`：发生断流，无法确认该分钟成交——OHLCV 全部置空，前端不进入 `dataList`，指标算子遇到它输出空值。

重连后按断流窗口起点回填 REST 历史；同一 `timestamp` 的多次发布按 `revision` 追加保留、最后一次写入生效（last-write-wins），重复回填是幂等的。

回放脚本为 JSONL，每行至少包含非负且非递减的 `offset_ms` 与 `type`（`kline` / `book` / `heartbeat` / `disconnect` / `reconnect`），K 线与盘口事件内嵌完整的契约对象：

```bash
# 回放 2026-11-01 的 36 条样本：8.5 秒断流 + 5 根 FEED_GAP 缺口 + 恢复
uv run talaria up --solo \
  --fixture-dir tests/fixtures/bybit \
  --replay-events tests/fixtures/realtime/demo_outage.jsonl

# 0.5 倍速便于观察断流计时
uv run talaria up --solo \
  --fixture-dir tests/fixtures/bybit \
  --replay-events tests/fixtures/realtime/demo_outage.jsonl \
  --replay-speed 0.5
```

`--replay-events` 可与 `--fixture-dir` 并用（fixture 只供历史快照）；文件缺失、不是普通文件或 `--replay-speed <= 0` 时命令以退出码 2 结束并输出中文提示。回放到文件末尾后不再产生 K 线，但会持续发送静默心跳，健康状态保持 `LIVE`。

## 快通道协议（`WS /ws/desk`）

前端通过由 `window.location` 推导的单一 WS 连接接收增量，断线后按 0.5s → 8s 指数退避自动重连，并重新订阅当前标的与周期。

服务端 → 客户端：

| `type` | 载荷 | 说明 |
| --- | --- | --- |
| `hello` | `{feed_mode, server_ts, live}` | 连接建立后立即推送；`live=false` 表示当前离线样本模式 |
| `bar` | `{data: KlineEvent}` | `confirmed=false` 为进行中 K 线，同 `timestamp` 以 `revision` 递增更新 |
| `book` | `{data: OrderBookEvent}` | 最优一档买卖价与量 |
| `health` | `{data: FeedHealthEvent}` | `LIVE` / `STALE` / `RECOVERING` 迁移与 `stale_since` |
| `error` | `{code, message}` | 订阅失败等错误 |

客户端 → 服务端：

```json
{"op": "subscribe", "instrument_id": "BTC-USDT-PERP", "timeframe": "1m"}
{"op": "unsubscribe", "instrument_id": "BTC-USDT-PERP", "timeframe": "1m"}
```

每个客户端有独立有界队列（256 条），队列满时丢弃最旧条目，任何慢客户端都不会阻塞其他客户端或行情写入。`GET /api/feed/health` 返回全通道聚合的 `FeedHealthReport`；fixture 模式没有实时流，返回 503 `FEED_UNAVAILABLE`。

## API 摘要

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/health` | `{ok, version, feed_mode}` |
| GET | `/api/instruments` | 四个加密永续标的 |
| POST | `/api/snapshots` | 创建 `HISTORICAL_PIT` 快照；`OBSERVED_REPLAY` 返回 501 |
| GET | `/api/snapshots/{id}` | 快照元数据 |
| GET | `/api/snapshots/{id}/bars?timeframe=&limit=&before_ts=` | 升序 `ChartPage`，`limit` 1..1000，含两个方向的 `has_more_*` |
| GET | `/api/feed/health` | `FeedHealthReport`；无实时流时 503 |
| WS | `/ws/desk` | `hello` / `bar` / `book` / `health` / `error` 增量与订阅控制 |

错误统一为 `{code, message}`：401 `UNAUTHORIZED`、404 `SNAPSHOT_NOT_FOUND`/`INSTRUMENT_NOT_FOUND`、422 `VALIDATION_ERROR`、501 `NOT_IMPLEMENTED`、503 `FEED_UNAVAILABLE`。

## 浏览器验收

验收使用 ego-browser，截图放在 `.scratch/tasks/ticket-03/`（已 gitignore）。启动带 `demo_outage.jsonl` 的实例后至少覆盖：

1. 1440×900 浅色：顶栏盘口与“实时”徽标，K 线随回放跳动；
2. 1440×900 深色：切换主题后图表、徽标与提示条配色同步；
3. 390×844 窄屏：无横向溢出，控件可换行但不裁切；
4. 1440×900 断流：徽标变红并显示“断流 N 秒”，断流提示条出现；
5. 1440×900 恢复：断流提示条消失，徽标回到“实时”，状态栏实时计数继续增长；
6. 390×844 断流或恢复：窄屏下提示条与计时同样完整可见。

## 架构边界

- `core/src/talaria/foundation`：L0 契约与协议，不知道任何具体市场或数据源；
- `core/src/talaria/perception`：原始柱落盘、缺口紧凑化、快照编排与读取；
- `core/src/talaria/feed`：`MarketEventFeed` 状态机（LIVE / STALE / RECOVERING、NO_TRADES 与 FEED_GAP 合成、重连回填）；
- `core/src/talaria/replay`：JSONL 确定性回放传输层，支持断流/恢复与可注入时钟；
- `core/src/talaria/packs/crypto_perp`：Bybit V5 Linear REST 连接器与公共 WebSocket 流、四个标的、24x7 `SessionTrait`；
- `core/src/talaria/workbench_api`：FastAPI 网关、Bearer 与静态托管；
- `dsh/packages/talaria-contracts`：由 Python 生成的 TypeScript 契约；
- `dsh/packages/talaria-desk-ui`：React 19 + KLineChart v10 看盘页。

指标（04）、人工画线（05）、冷库发布（06）、衍生品（07）、自选池（08）、DSH 接入（09）、关键位与预警（10+）不在本单范围。

## 第三方

KLineChart 10.1.1（Apache-2.0）与 `websockets` 15.x（BSD-3-Clause）的许可证归属见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
