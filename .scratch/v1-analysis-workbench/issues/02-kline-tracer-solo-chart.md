# 02 — 最窄贯通：Bybit 历史 K 线回填、证据快照冻结与 `--solo` 看盘页显示 K 线

**What to build:** 交易员执行 `talaria up --solo`，浏览器打开本地看盘页，选择一个 `Bybit V5 Linear` 永续 `Instrument` 和周期后看到由 Python 守护进程回填的历史 K 线，向左拖动时分页加载更早数据；开发者可按截止时刻冻结 `Evidence Snapshot` 并按 `snapshot_id` 读回同一份 K 线。这是贯穿契约、连接器、存储、快照、快通道接口与图表的第一条最窄路径，后续任务都在它上面加宽。

**Blocked by:** 01 — 核实并锁定外部依赖版本与许可证（只需第 2 项 KLineChart 结论）

**Source of truth:** [需求文档](../spec.md)、[领域词汇表](../../../GLOSSARY.md)、[系统分层架构规范](../../../docs/architecture.md)、[ADR-0001](../../../docs/adr/0001-dual-plane-dsh-and-python-core.md)、[ADR-0002](../../../docs/adr/0002-capability-traits-and-market-packs.md)、[ADR-0005](../../../docs/adr/0005-fast-path-bypass-and-decoupled-chart-workbench.md)、[ADR-0008](../../../docs/adr/0008-analysis-first-v1-and-deterministic-key-level-engine.md)

**Status:** resolved

- [x] 跨语言契约检查命令可独立运行并断言：价格、数量、费率在 JSON Schema 中一律为十进制字符串；Python 导出契约与前端生成类型零漂移；面向智能体工具导出的 Schema 子集不含根级 `$schema`、无未展开 `$defs`。
- [x] 底座契约表达五重时点（`Event Time`、`ts_published`、`Available Time` + 精度等级、`ts_ingested`、`Committed Time`）、通用 `Instrument` 规格、五项能力特征接口，以及 K 线柱状态（正常 / `NO_TRADES` / `FEED_GAP`）；核心层无特定市场条件分支，`Bybit V5 Linear` 连接器与 `24x7` `SessionTrait` 通过加密永续 `Market Pack` 注册。
- [x] 连接器免密钥分页回填多周期 K 线；数据以最简单的方式落盘（冷库原子发布流程由 06 补齐）。
- [x] `get_snapshot` 按截止时刻冻结 `Evidence Snapshot`，`HISTORICAL_PIT` 查询只返回 `Available Time <= cutoff_ts` 的数据并输出紧凑交易柱序列；`OBSERVED_REPLAY` 参数存在但返回明确的未实现错误；`Catalyst Calendar` 查询为空实现并遵守防前视裁剪语义。
- [x] `talaria up --solo` 启动仅绑定 `127.0.0.1` 的守护进程并托管纯 React + `KLineChart v10` 页面；写操作接口校验启动时随机生成的会话 Bearer Token（缺失或错误返回 `401`）；收到退出信号时优雅退出。
- [x] 图表通过历史分页接口加载 K 线，十进制字符串只在画布绘制最后一跳转为浮点数。
- [x] 离线确定性样本测试覆盖时点截断、快照读回与鉴权；访问真实 Bybit 在线接口的连通性检查标记为沙箱外可选冒烟验证。
- [x] 浏览器界面验收（均附截图）：宽度 `1440` 与 `390`、浅色与深色、简体中文下检查 K 线主图与标的/周期切换；覆盖长合约标识不溢出，以及尚未加载到历史行情时的空数据引导态。

## Comments

### 2026-11-19 实现摘要

- 分支：`ticket-02-kline-tracer`（从 main `5d2cf81` 创建）；未合并、未推送。
- 交付：uv workspace + `core/src/talaria`（foundation / perception / packs.crypto_perp / workbench_api / cli）、`contracts/schemas` 与 DSH 子集、React 19 + Vite + KLineChart 10.1.1 看盘页、确定性 Bybit fixture、按模块测试、README/THIRD_PARTY_NOTICES。
- 契约：`DecimalStr` 在 JSON Schema 中为 string+pattern；`TemporalStamp` 五重时点与两个次序校验；五项 Trait；`CompactBar` 的 NORMAL/NO_TRADES/FEED_GAP；`HISTORICAL_PIT` 快照与 `OBSERVED_REPLAY` 501；`CatalystCalendar` 防前视裁剪。
- 数据：`$TALARIA_DATA_DIR`（默认 `./.talaria`）；原始柱 JSONL fsync 追加、按 timestamp last-ingested-wins；快照 Parquet 原子写定 + SQLite WAL 清单；读取只使用清单显式路径。
- 守护进程：`talaria up --solo` 只接受回环 IP，默认 `127.0.0.1:8765`；随机 Bearer token（`/desk` 下发 SameSite=Strict、HttpOnly=false cookie）；`/api` 写操作缺失/错误 token 返回 401；无 CORS；SIGINT/SIGTERM 优雅退出。
- 前端：简体中文、`zh-CN`/`Asia/Shanghai`、浅/深色与 localStorage、响应式；`toKLineData` 是唯一的 DecimalString→Number 转换点，FEED_GAP 过滤并在状态栏计数；空态 `empty-state` 与加载态。

验证命令与结果（均通过）：

```bash
uv run pytest -q
# 85 passed, 1 deselected (online), 1 warning (Starlette TestClient httpx 弃用)

pnpm -C dsh --filter @talaria/desk-ui test
# 6 passed
pnpm -C dsh typecheck
# 2 个 workspace 包通过
pnpm -C dsh build
# 成功：JS 474.87 kB / CSS 7.73 kB / index.html 0.41 kB

uv run talaria contract-check
# 契约生成物零漂移。exit 0

git diff --cached --check
# exit 0
```

浏览器验收（ego-browser，截图已 `read_image` 确认非空白）：

- `01-1440-light.png`：1440x900 浅色，BTC-USDT-PERP/1m，已加载 295 根、断流 5 处，`scrollWidth=1440`。
- `02-1440-dark.png`：1440x900 深色，切换主题后图表同步，已加载 295 根。
- `03-390-narrow.png`：390x844，ETH-USDT-PERP/5m，无横向溢出（`scrollWidth=390`），可切换标的/周期。
- `04-long-instrument-390.png`：390x844，`1000BONK-USDT-PERP`，ellipsis + title，无溢出。
- `05-forward-pagination.png`：1440x900，向左拖动触发 forward，`已加载 295 根` → `595 根`。
- `06-empty-state.png`：8766 实例 + 不可达 `--bybit-base-url`，`empty-state` 可见、图表隐藏、错误 `FEED_UNAVAILABLE`。

截图目录：`.scratch/tasks/ticket-02/`（已 gitignore）。

已知限制/风险：

- 本机网络访问真实 Bybit 为 CloudFront 403，在线连通性未验证；`tests/packs/test_online_bybit.py` 标记 `online`，默认跳过，沙箱外可选冒烟。
- 图表暂时过滤 `FEED_GAP` 仅在状态栏计数；API 完整保留这些柱。后续 03/04 可在不重写快照层的前提下接入断流与指标。
- `BybitLinearConnector` 自建 httpx client 使用 `trust_env=False`，不继承环境代理；需要代理的部署应显式注入 client。
- `OBSERVED_REPLAY`、实时流、指标、画线、冷库发布、衍生品、自选池、DSH 接入、关键位与预警均按任务单留给后续。

### 2026-11-19 合并验收（lead）

- 合并：`ticket-02-kline-tracer`（`6a6959e`）以 `7714a09` 合入 main，87 files / +8077 −8；工作树已删除，分支已删。
- main 重跑：`uv run pytest -q` → 85 passed, 1 deselected（`online` 默认跳过）；`uv run talaria contract-check` → 零漂移；`pnpm -C dsh typecheck` → 2 包通过；`pnpm -C dsh --filter @talaria/desk-ui test` → 6 passed；`pnpm -C dsh build` → 成功。
- 手动冒烟（`.venv/bin/python -m talaria up --solo --fixture-dir tests/fixtures/bybit`）：`/api/health` 返回 `feed_mode=fixture`；`/api/instruments` 4 个标的；`POST /api/snapshots` 无 token 401、带 token 200 且 `bar_counts.1m=905`；`GET .../bars?limit=5` 返回十进制字符串与 `SECOND_VERIFIED`；SIGTERM 后约 0.4s 完成 shutdown，端口释放。
- 验收截图已复制到主仓库 `.scratch/tasks/ticket-02/`（01–06 六张，均已检查非空白）。
- 设计说明：五重时点由独立 `TemporalStamp` 契约表达并测试；紧凑柱只携带 `ts_available` + `precision`，避免把审计时点塞进图表数据。`get_bars` 的 `has_more_forward` 指向更早数据、`has_more_backward` 指向更新数据，已有分页测试覆盖。
- 遗留：图表当前过滤 `FEED_GAP` 并仅在状态栏计数，与 `docs/architecture.md` 的“紧凑序列与 dataList 1:1”存在张力；04 接入指标时须让指标序列与图表过滤后的柱严格对齐，或统一改为 1:1 表示。`MAX_GAP_FILL=1000` 的超大缺口不展开，06 冷库发布时复核。
