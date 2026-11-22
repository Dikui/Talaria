# Talaria

Talaria 是交易员的本地分析工作台。Python 底座负责历史回填、快照冻结、实时行情、指标、关键位、预警与扫描。React 工作台提供看盘和标注，DSH 插件提供人机协同会诊与扫描简报。

## 环境

- Python 3.14+，[uv](https://docs.astral.sh/uv/)
- Node.js 26+，pnpm 11+
- 前端图表锁定 `klinecharts@10.1.1`

## 启动工作台

首次启动前安装依赖并构建前端。

```bash
uv sync
pnpm -C dsh install
pnpm -C dsh build
```

在 macOS 安装 DeepSeek Harness 桌面应用（基线 0.2.2-rc.5）。首次打开应用，让它初始化 `desktop` profile，然后完全退出应用，再安装工作台插件和登录自启的常驻服务。

```bash
uv run talaria dsh-install
uv run talaria service install
uv run talaria open
```

日常用 `talaria open` 打开桌面应用，从侧边栏进入图表、预警、扫描和关键位。未安装桌面应用时会打开常驻服务托管的 `/desk`。用 `talaria service status` 查看常驻服务、桌面应用与 Talaria 插件状态。升级已安装的常驻服务时，再运行一次 `talaria service install` 更新 launchd 作业。

如果常驻服务使用自定义数据目录，给 `dsh-install`、`service install` 和 `open` 传入同一个 `--data-dir`。省略时，`dsh-install` 和 `open` 依次读取 `TALARIA_DATA_DIR`、默认 launchd 作业及 `./.talaria`。

### 离线确定性样本

```bash
# 只启动 Python 看盘守护进程，使用仓库内的 Bybit fixture
uv run talaria up --solo --pack crypto_perp --fixture-dir tests/fixtures/bybit
```

浏览器打开 <http://127.0.0.1:8765/desk>。`/desk` 会把随机会话 token 写入同源的 `talaria_token` cookie；前端读取后放入 `Authorization: Bearer`，所有写操作都必须带这个头。

`--fixture-dir` 只提供历史行情。单独使用时，`/api/feed/health` 返回 503，顶栏显示“离线样本”。要查看实时更新和断流健康状态，可同时使用下文的本地回放。

`talaria up` 与 `talaria evaluate-levels` 都通过 `--pack` 选择已注册的市场包，默认 `crypto_perp`。原始行情目录由市场包的 `raw_archive_id` 决定，`crypto_perp` 继续使用 `bybit_linear`，已有归档可直接复用。

`talaria up` 优先使用 `--data-dir`，其次是 `TALARIA_DATA_DIR`，默认 `./.talaria`。主要数据路径如下。

```text
<data-dir>/data/raw/<archive_id>/<SYMBOL>/<timeframe>.jsonl            # 历史回填原始柱
<data-dir>/data/raw_archive/<sha256>.jsonl                            # 冷库批次的哈希归档
<data-dir>/data/cold/<venue>/<symbol>/<timeframe>/<sha256>.parquet     # 冷库 generation
<data-dir>/data/data_manifest.sqlite                                 # 冷库清单与游标
<data-dir>/state/runtime_snapshots.sqlite                            # 快照索引
<data-dir>/state/hot_snapshots/<snapshot_id>.parquet                  # 冻结快照
<data-dir>/state/collaboration_state.sqlite                          # 自选、预警、标注与协同状态
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

## 关键位离线评估

`evaluate-levels` 逐时点重放关键位引擎：每次只用 `ts_available <= cutoff` 的柱，统计每个 `kind` 被触及后 `horizon_bars` 根内出现 `atr_multiple × ATR` 反应的比例与平均幅度，并与同窗口、固定种子的随机价位基线对照。ATR 复用 `foundation/operators` 的 ATR（默认周期 14）。

```bash
uv run talaria evaluate-levels \
  --pack crypto_perp \
  --instrument BTC-USDT-PERP \
  --timeframe 1m \
  --from 2026-11-01T08:00:00+08:00 \
  --to 2026-11-01T18:00:00+08:00 \
  --atr-multiple 1.0 \
  --horizon-bars 20 \
  --baseline-samples 200 \
  --seed 42
```

- `--from` / `--to` 接受毫秒时间戳或 ISO-8601；不带时区的 ISO 按 `Asia/Shanghai` 解释，带 `Z` 或偏移量的按其自身时区。
- 默认向 stdout 输出 JSON，同时向 stderr 输出可读表格与数据源；`--no-json` 只输出表格。错误以退出码 2 和 stderr JSON `{"error": {"code", "message"}}` 返回。
- 显式传入 `--fixture-dir` 时从该目录读取。只给 `--data-dir` 时读取市场包的原始存档，`crypto_perp` 对应 `<data-dir>/data/raw/bybit_linear/<SYMBOL>/<timeframe>.jsonl`。两个参数都省略时使用市场包提供的默认 fixture，`crypto_perp` 对应 `tests/fixtures/bybit`。
- 输出字段：输入参数 `instrument_id`、`timeframe`、`from_ts` / `to_ts`、`atr_period`、`atr_multiple`、`horizon_bars`、`baseline_samples`、`seed`，以及 `method_version`、`reaction_definition`、`skipped`、`baseline_skipped`、`total_sample_count`、`baseline_sample_count` 和 `results[]`。每个 `kind` 含 `sample_count`、`reaction_count`、`reaction_rate`、`mean_reaction_atr`、`skipped_count`、`baseline_sample_count`、`baseline_reaction_count`、`baseline_reaction_rate`、`baseline_mean_reaction_atr`、`baseline_skipped_count`。
- 样本：同一 `level_id` 跨 cutoff 只计一次，取它在评估窗口内的首次触及；触及指柱的 high/low 包含线价（区间位为区间重叠）。触及之后严格后续 `horizon_bars` 根内，任一 close 相对价位/区间达到阈值即算反应（向上突破与向下反弹都算）；`mean_reaction_atr` 是最大偏离的 ATR 倍数均值。
- 随机基线：从同窗口收盘价分布抽 `baseline_samples` 个价位，每个 `kind` 用 `sha256(seed:kind)` 派生独立随机流；触及与反应判定和真实关键位完全相同，但不读取引擎的 `level_id`、状态或强度。
- 首次触及后数据不足（包括 ATR 预热不足）的样本计入 `skipped`（真实关键位）或 `baseline_skipped`（随机基线），不会以 0 代替。
- 类别由 `KeyLevelKind` 自动枚举，包括成交量位、锚定 VWAP、清算估算等。

## 真实 Bybit（可选）

在可访问 Bybit 的环境中，可运行在线冒烟测试或启动实时看盘。

```bash
uv run pytest -m online tests/packs/test_online_bybit.py
uv run talaria up --solo --pack crypto_perp --history-base-url https://api.bybit.com
```

`--history-base-url` 可覆盖市场包默认的历史接口地址，`--bybit-base-url` 仍可作为兼容别名使用。`talaria up` 只接受回环 IP，工作台端口默认 8765。

开发和自动化测试可先运行 `talaria dsh-install --dsh-profile talaria-desk`，再运行 `talaria up --pack crypto_perp` 启动服务与独立 DSH Web 宿主（默认端口 8766）。用终端打印的宿主地址进入开发工作台。宿主离线时，`/desk` 继续提供本地看盘。

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
uv run talaria up --solo --pack crypto_perp \
  --fixture-dir tests/fixtures/bybit \
  --replay-events tests/fixtures/realtime/demo_outage.jsonl

# 0.5 倍速便于观察断流计时
uv run talaria up --solo --pack crypto_perp \
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
| GET | `/api/instruments` | 所选市场包的标的，`crypto_perp` 提供四个加密永续标的 |
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

- `core/src/talaria/foundation`：L0 契约与协议、纯 Python `Decimal` 指标算子和关键位纯函数。
- `core/src/talaria/perception`：原始柱落盘、缺口紧凑化、快照冻结、冷库发布、预警与扫描引擎。
- `core/src/talaria/feed`：`MarketEventFeed` 状态机（LIVE / STALE / RECOVERING、NO_TRADES 与 FEED_GAP 合成、重连回填）。
- `core/src/talaria/replay`：JSONL 确定性回放传输层，支持断流/恢复与可注入时钟。
- `core/src/talaria/evaluation`：离线关键位质量评估，供 `evaluate-levels` 调用。
- `core/src/talaria/packs`：市场包注册表。当前 `crypto_perp` 提供 Bybit V5 Linear 行情连接器、标的与领域技能。
- `core/src/talaria/workbench_api`：FastAPI 网关、运行时组装、领域路由、WS 广播与协同状态库。
- `dsh/packages/talaria-contracts`：由 Python 生成的 TypeScript 契约。
- `dsh/packages/talaria-desk-ui`：React 19 + KLineChart v10 看盘页。
- `dsh/packages/talaria-dsh-plugin`：DSH 工作台插槽、分析工具与会诊桥接。

交易闭环与交易回放规划于 M3，智能体自治与评估、参数寻优、美股市场包规划于 M4。分层与实现位置见[架构规范](docs/architecture.md)。

## 第三方

KLineChart 10.1.1（Apache-2.0）与 `websockets` 15.x（BSD-3-Clause）的许可证归属见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
