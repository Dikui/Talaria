# Talaria

Talaria 是交易员的本地分析工作台：Python 底座负责历史行情回填、快照冻结与契约，React 前端负责 K 线与分析交互。当前版本对应任务单 02，只实现 `--solo` 只读看盘贯通。

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

## API 摘要

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/health` | `{ok, version, feed_mode}` |
| GET | `/api/instruments` | 四个加密永续标的 |
| POST | `/api/snapshots` | 创建 `HISTORICAL_PIT` 快照；`OBSERVED_REPLAY` 返回 501 |
| GET | `/api/snapshots/{id}` | 快照元数据 |
| GET | `/api/snapshots/{id}/bars?timeframe=&limit=&before_ts=` | 升序 `ChartPage`，`limit` 1..1000，含两个方向的 `has_more_*` |

错误统一为 `{code, message}`：401 `UNAUTHORIZED`、404 `SNAPSHOT_NOT_FOUND`/`INSTRUMENT_NOT_FOUND`、422 `VALIDATION_ERROR`、501 `NOT_IMPLEMENTED`、503 `FEED_UNAVAILABLE`。

## 浏览器验收

验收使用 ego-browser，截图放在 `.scratch/tasks/ticket-02/`（已 gitignore）。启动 fixture 守护进程后至少覆盖：

1. 1440×900 浅色，K 线主图与标的/周期控件正常；
2. 1440×900 深色，切换后图表配色同步；
3. 390×844 窄屏，无横向溢出且可切换标的/周期；
4. `1000BONK-USDT-PERP` 长标识以 ellipsis 收束；
5. 向左拖动触发 forward 分页，“已加载 N 根”增加；
6. 使用不可达 `--bybit-base-url` 或空 fixture 启动另一实例，看到 `empty-state` 引导卡。

## 架构边界

- `core/src/talaria/foundation`：L0 契约与协议，不知道任何具体市场或数据源；
- `core/src/talaria/perception`：原始柱落盘、缺口紧凑化、快照编排与读取；
- `core/src/talaria/packs/crypto_perp`：Bybit V5 Linear 连接器、四个标的、24x7 `SessionTrait`；
- `core/src/talaria/workbench_api`：FastAPI 网关、Bearer 与静态托管；
- `dsh/packages/talaria-contracts`：由 Python 生成的 TypeScript 契约；
- `dsh/packages/talaria-desk-ui`：React 19 + KLineChart v10 看盘页。

实时流（03）、指标（04）、人工画线（05）、冷库发布（06）、衍生品（07）、自选池（08）、DSH 接入（09）、关键位与预警（10+）不在本单范围。

## 第三方

KLineChart 10.1.1（Apache-2.0）的许可证与 NOTICE 归属见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
