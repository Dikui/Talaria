# 07 — 永续衍生品数据（`Mark Price` / `Funding Settlement` / 持仓量）与衍生品指标

**What to build:** 交易员在看盘页看到当前 `Instrument` 的标记价格、实时资金费率、下次结算时间与持仓量，并可在副图打开资金费率分位数与持仓量背离度指标；这些数据进入 `Evidence Snapshot`，供关键位清算估算、预警与智能体读取。

**Blocked by:** 03 — 实时 K 线与盘口流、`NO_TRADES` / `STALE` 断流判定与顶栏健康提示；04 — 单一实现技术指标算子核与图表主副图指标

**Source of truth:** [需求文档](../spec.md)、[领域词汇表](../../../GLOSSARY.md)、[系统分层架构规范](../../../docs/architecture.md)、[ADR-0002](../../../docs/adr/0002-capability-traits-and-market-packs.md)、[ADR-0008](../../../docs/adr/0008-analysis-first-v1-and-deterministic-key-level-engine.md)

**Status:** resolved

- [x] 连接器免密钥回填 `Mark Price` K 线、`Funding Settlement` 历史与 `5min+` 持仓量（游标翻页），并订阅实时 `Mark Price`、资金费率与持仓量；衍生品特征通过加密永续 `Market Pack` 挂载，核心层无市场分支。
- [x] 上述数据带五重时点进入快照，`HISTORICAL_PIT` 截断正确。
- [x] 在 04 的算子核中加入资金费率分位数、持仓量背离度与净赔率几何验算，遵守同一套等长 / 预热 / 断流语义并补充黄金向量。
- [x] 快通道 WebSocket 推送衍生品实时值；看盘页顶栏或侧栏展示，副图可开衍生品指标。
- [x] 浏览器界面验收（均附截图）：宽度 `1440` 与 `390`、浅色与深色、简体中文下检查衍生品状态区与衍生品副图；覆盖数值缺失（新上市标的无持仓量历史）时的空态。

## Comments

### 实现摘要

- **Market Pack / 连接器**：`CryptoPerpPack` 通过 `DerivativeCapabilityTrait(kinds=MARK_PRICE/FUNDING_RATE/OPEN_INTEREST, history=True, realtime=True)` 声明能力；`foundation`/`perception` 只依赖 `DerivativeDataConnector` 协议与能力 trait，无 Bybit 或市场分支。`BybitLinearConnector` 增加 `/v5/market/mark-price-kline`、`/v5/market/funding/history`、`/v5/market/open-interest`，支持 `end`/`endTime` 游标、`limit<=1000`、剔除未收盘 K 线；`BybitPublicStream` 首次订阅某标的时追加 `tickers.{symbol}`，解析 `markPrice`/`fundingRate`/`nextFundingTime`/`openInterest` 为 `DerivativeTickEvent`。`FixtureConnector` 离线读取 `tests/fixtures/bybit/<SYMBOL>/derivatives/*.json`，浏览器验收不依赖真实 Bybit。
- **快照 / PIT**：衍生品点带 `ts_event/ts_published/ts_available(+precision)/ts_ingested/ts_committed` 五重时点。`SnapshotBuilder` 回填后写入 manifest 登记的 sidecar `state/hot_snapshots/<id>.derivatives.parquet`，`SnapshotStore` 记录 `derivatives_path`；读取端仅通过快照 ID 解析 manifest 路径，不扫描目录。`HISTORICAL_PIT` 在构建、读取、API 三层都以 `ts_available <= cutoff_ts` 严格过滤；`OBSERVED_REPLAY` 仍返回 501。
- **算子核**：`compute_series`/`update_series` 增加可选 `derivatives: Sequence[DerivativeSample]`，三个算子与既有算子共享 N-in/N-out、前导 null、`NO_TRADES` 持有上一点、`FEED_GAP` 立即 null 并全量重新预热、批量=增量语义。黄金向量 `contracts/operator_vectors/{funding_quantile,oi_divergence,net_odds}.json` 含 12 根 K 线、`NO_TRADES`、`FEED_GAP` 与并列衍生品样本，回归覆盖批量/增量及断流。
- **快通道 / API**：`GET /api/snapshots/{id}/derivatives?timeframe=` 返回 `DerivativeBundle`（三条序列 + 摘要，Decimal 序列化为字符串）；指标与算子接口对衍生品算子自动按 `ts_available` 做 as-of 对齐。WS 新增 `{"type":"derivatives","data":...}`，`bar`/`book`/`health` 不变。
- **前端**：`DerivativeSummary` 在顶栏展示标记价、资金费率、下次结算倒计时、持仓量；`INDICATOR_ORDER` 增加资金费率分位与 OI 背离，经 04 现有 pane 机制开副图；无 OI 历史时摘要显示 `暂无 OI 历史`，OI 副图显示空态文案；全部指标值来自后端，无前端计算。390 宽度沿用 04/05 的 `isReadOnlyViewport` 隐藏编辑控件。

### 公式与参数

- **资金费率分位数 `FUNDING_QUANTILE`（默认 period=30，输出 [0,1]）**：对每根 K 线取 `ts_available` as-of 的最新资金费率，在包含当前值的最近 `period` 个 K 线样本上计算百分位秩 `(count_less + 0.5*count_equal) / n`，量化 8 位小数。同一结算费率在多个 K 线上重复时按时间加权（每个 K 线样本各计一次）。
- **持仓量背离度 `OI_DIVERGENCE`（默认 period=30）**：`(close_t / close_{t-period} - 1) - (oi_t / oi_{t-period} - 1)`，OI 取 `ts_available` as-of 的最新值（1m K 线映射到 5m OI），量化 8 位小数。
- **净赔率几何验算 `NET_ODDS`（默认 period=20）**：`(close_t - close_{t-period} - Σ(funding_rate * close_t)) / ATR_simple(period)`，其中资金成本累计窗口为 `(t-period 的 K 线时间, t]` 内已发生的结算，ATR 为最近 `period` 个真实波幅的简单均值，作为几何风险单位；量化 6 位小数。资金费率为正时降低多头净赔率。
- 三个算子均为逐根输出，前 `period-1` 根为 null；K 线 `NO_TRADES` 不推进引擎并复制上一点，`FEED_GAP` 清空窗口、输出 null 后重新预热；`update_series` 与 `compute_series` 共用同一引擎，保证批量/增量一致。

### 测试

- `uv run pytest -q` → **289 passed, 1 deselected**（新增衍生品连接器、tickers 流、supervisor、PIT 快照、衍生品 API、算子向量与断流回归；`online` 用例默认跳过）。
- `uv run talaria contract-check` → **契约生成物零漂移**。
- `pnpm -C dsh typecheck` → **通过**。
- `pnpm -C dsh --filter @talaria/desk-ui test` → **14 files / 87 tests passed**。
- `pnpm -C dsh build` → **成功**（`dist/assets/index-CHWGjSzz.js`）。

### 浏览器验收（ego-browser，fixture daemon `http://127.0.0.1:8899/desk`）

- 1440 浅色衍生品状态区：`/Users/dikui/Project/Talaria/.agents/worktrees/ticket-07-derivatives/.scratch/tasks/ticket-07/1440-light-derivatives.png`（标记价、资金费率、下次结算倒计时、持仓量均展示；`read_image` 确认非空）。
- 1440 深色衍生品副图：`/Users/dikui/Project/Talaria/.agents/worktrees/ticket-07-derivatives/.scratch/tasks/ticket-07/1440-dark-derivative-panes.png`（RSI、ATR、`FUNDING_QUANTILE(30)=0.50`、`OI_DIVERGENCE(30)=0.00` 四个副图，主图未塌陷）。
- 390 只读：`/Users/dikui/Project/Talaria/.agents/worktrees/ticket-07-derivatives/.scratch/tasks/ticket-07/390-readonly.png`（`scrollWidth=390`，画图工具栏/检查器/指标参数编辑均隐藏，衍生品状态区仅保留标记价与资金费率）。
- 新上市无 OI 空态：`/Users/dikui/Project/Talaria/.agents/worktrees/ticket-07-derivatives/.scratch/tasks/ticket-07/1440-newlisting-no-oi.png`（`1000BONK-USDT-PERP`，摘要 `标记价 0.023552 / 资金费率 — / 下次结算 — / 持仓量 暂无 OI 历史`，副图空态文案 `该合约暂无持仓量历史，OI 背离暂不可计算。`）。
- 截图目录已 gitignore，未入库。

### 已知限制

- 本机网络对 Bybit 域名受 CloudFront 地域限制，真实联调用例标记 `@pytest.mark.online` 并默认跳过；验收与回归全部使用确定性 fixture，未验证真实交易所时延/限频行为。
- 资金费率分位默认周期取 30（而非 120），因 fixture 在断流后需全量重新预热，120 根窗口会使最新点长时间为 null；该参数可由指标请求覆盖。
- 快照中的下次结算时间按 `Funding Settlement` 的 `ts_event + 8h` 与快照 cutoff 推算首个未来结算，不依赖实时 WS 的 `nextFundingTime`；实时值到达后前端会覆盖摘要。
- 衍生品 sidecar 与 K 线 parquet 分文件存储，但读取统一经 `SnapshotStore` 清单，未引入新的旁路读取方式。
- 本分支未合并、未推送；`Status` 保持 `ready-for-agent`，由 lead 在合并后处理。


### Lead 合并验收（2026-11-19）

- 合并 `ba59350 Merge branch 'ticket-07-derivatives'`，与 09 的 `App.tsx`/`styles.css`/`api.ts` 自动合并成功，无冲突；`contract-check` 零漂移。
- 合并后全量：`uv run pytest -q` → 303 passed, 1 deselected；`pnpm -C dsh typecheck` 通过；desk-ui 87 passed；plugin 12 passed；`pnpm -C dsh build` 成功。
- 目视复核截图：1440 浅色顶栏显示标记价 68700.9、资金费率 -0.0200%、下次结算倒计时、持仓量 100,125.17；1440 深色可同时开 `FUNDING_QUANTILE(30)` 与 `OI_DIVERGENCE(30)` 副图，主图未塌陷；`1000BONK-USDT-PERP` 无 OI 时显示“该合约暂无持仓量历史，OI 背离暂不可计算。”空态；390 只读保留状态区最小读数。
- 接受已知限制：真实 Bybit 联网未验证（CloudFront 地域限制，fixture 回归）；资金费率分位默认 30；`next_funding_ts` 由历史结算 + 8h 推算，实时 WS 值覆盖，非严格 PIT；衍生品 sidecar 与 K 线分文件但统一走 `SnapshotStore` 清单。
- 截图已复制到 `.scratch/tasks/ticket-07/`（4 张）；worktree 与分支在关闭后删除。
