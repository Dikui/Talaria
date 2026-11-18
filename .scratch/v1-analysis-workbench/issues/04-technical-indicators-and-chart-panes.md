# 04 — 单一实现技术指标算子核与图表主副图指标

**What to build:** 交易员在看盘页打开 EMA、SMA、RSI、ATR、BOLL、VWAP 等指标，主图叠加和副图窗格显示的数值全部来自 Python 底座的唯一一份算子实现；开发者和后续智能体工具可通过同一接口读取默认指标序列或用非默认参数临时计算。衍生品指标在 07 中加入。

**Blocked by:** 02 — 最窄贯通：Bybit 历史 K 线回填、证据快照冻结与 `--solo` 看盘页显示 K 线

**Source of truth:** [需求文档](../spec.md)、[领域词汇表](../../../GLOSSARY.md)、[系统分层架构规范](../../../docs/architecture.md)、[ADR-0008](../../../docs/adr/0008-analysis-first-v1-and-deterministic-key-level-engine.md)

**Status:** resolved

- [x] 纯函数算子面向紧凑交易柱序列：输入长度 N 输出等长 N，预热期输出前导空值；`NO_TRADES` 柱保态递推；遇 `FEED_GAP` 立即输出空值并重跑完整预热期（以 02 契约中的柱状态驱动，可用注入数据测试，不依赖 03）。批量计算与增量更新结果一致。
- [x] 黄金测试向量固化各算子的标准输入、前导空值长度、`NO_TRADES` 递推与 `FEED_GAP` 重预热结果，纳入契约回归。
- [x] 快通道开放绑定 `snapshot_id` 的 `get_indicators`（默认最近窗口序列与摘要）与 `compute_operator`（非默认参数），返回十进制字符串结构化结果。
- [x] 图表通过 `KLineChart v10` 自定义指标渲染接口消费后端序列，支持多指标开关、周期切换与参数配置；前端无指标数学计算代码（以搜索无 EMA/RSI 等计算实现为准）。03 完成后，实时增量指标随 WebSocket 推送更新。
- [x] 浏览器界面验收（均附截图）：宽度 `1440` 与 `390`、浅色与深色、简体中文下检查指标选择器、主图叠加与多副图堆叠；覆盖长参数图例折行 / 折叠，以及 K 线不足预热期或 `FEED_GAP` 空值区间的断线展示。`390` 下指标可查看，参数配置可隐藏。

## Comments

### 实现（2026-11-19，分支 `ticket-04-indicators`）

- 算子核：新增 `core/src/talaria/foundation/operators/`，实现 EMA/SMA/RSI/ATR/BOLL/VWAP 纯函数，输入/输出严格等长；`Decimal` 计算，`NO_TRADES` 保态，`FEED_GAP` 当根 `None` 并从下一根重跑完整预热；`compute_series` 与 `update_series` 共用同一递推器，全部切分点逐值一致。RSI/ATR 使用 Wilder 平滑，BOLL 使用总体标准差，VWAP 支持快照起点/UTC 日界锚点。
- 契约：新增 `core/src/talaria/foundation/indicators.py`，登记 `contracts_io.SCHEMA_MODELS`/`ENUM_TYPES`/`DECIMAL_FIELDS`；生成 `contracts/schemas/indicator_*.json`、`operator_request.schema.json` 与 `dsh/packages/talaria-contracts/src/generated.ts`。`IndicatorParams` 含 `period`、BOLL `std_dev`、VWAP `anchor`。
- 黄金向量：`contracts/operator_vectors/{sma,ema,rsi,atr,boll,vwap}.json`，覆盖前导空值长度、NO_TRADES 保态、FEED_GAP 空值+重预热、批量/增量一致；`tests/foundation/operators/` 逐值断言。
- API：`GET /api/snapshots/{snapshot_id}/indicators`（默认最近 300 点，`limit` 1..5000）与 `POST /api/operators/compute`（Bearer 写鉴权）；默认绑定冻结快照并只取 `ts_available <= cutoff`，未知快照/周期 404、参数错误 422。
- 前端：`MarketChart` 注册两个 KLineChart v10 自定义指标模板，主图指标显式挂 `candle_pane` 并用 `isStack=true` 叠加，RSI/ATR 独立副图；`calc` 只按 `Map<timestamp, values>` 对齐 `dataList`，缺失/断线为 `null`，不补 0。支持开关、周期切换、period/BOLL 倍数/VWAP 锚点配置；390 下保留查看、隐藏参数设置；长图例折行/折叠。
- 实时增量：WS bar 触发 550ms 去抖后带 `live=1` 查询；后端用 `FeedSupervisor.recent_bars` 追加冻结快照之后的实时尾段，并在 Python 中重算。快照历史仍来自 Python 冻结快照。
- 依赖与许可证：`TA-Lib==0.8.3`、`talipp==2.7.0` 已锁定并由 `uv lock` 固化；`THIRD_PARTY_NOTICES.md` 补充许可证。当前运行时以纯 Python 算子核为唯一权威，二者仅作为后续可选交叉校验基准。

### 验证

- `uv run pytest -q` → 185 passed, 1 deselected（仅 Starlette httpx 弃用 warning）。
- `uv run talaria contract-check` → 契约生成物零漂移。
- `pnpm -C dsh typecheck`、`pnpm -C dsh --filter @talaria/desk-ui test`（42 passed）、`pnpm -C dsh build` 全绿。
- `rg` 检查 `dsh/packages/talaria-desk-ui/src`：无 `avg_gain`/`alpha`/`variance`/`sqrt`/`true_range` 等指标公式；`Number(raw)` 仅出现在 `src/lib/indicatorData.ts:41` 的 Canvas 最后一跳，`Math.min/max` 仅用于 period 输入钳制。

### 浏览器验收（`.scratch/tasks/ticket-04/`，均为 1440 或 390 宽，已逐张 `read_image`）

1. `11-1440-settings-multi.png`（1440x1100，深色）：设置面板展开，EMA/SMA/BOLL/VWAP 主图叠加 + RSI/ATR 两个副图，共 3 个 pane；EMA(50)/SMA(100)/BOLL(26,2.5σ)/RSI(21)/ATR(21) 参数生效，长图例折行。
2. `12-1440-light-six.png`（1440x1024，浅色）：6 指标、2 副图，ATR 与折叠参数换行。
3. `13-1440-dark-six.png`（1440x1056，深色）：同上深色主题。
4. `14-1440-gap-warmup.png`（1440x950，深色）：仅 EMA(50)，预热不足与 FEED_GAP 后空值区间明显断线，未用 0 填充；状态栏显示断流 5 处。
5. `15-390-light-six.png`（390x1251，浅色，整页）：6 指标可查看；`document.documentElement.scrollWidth=390`、`body.scrollWidth=390`，设置区 `display:none`，无横向溢出。
6. `16-390-dark-six.png`（390x1251，深色，整页）：同断言，深色只读浏览。
7. `17-1440-5m-indicators.png`（1440x1024，深色）：切 5m 后快照重建、6 指标保持，RSI/ATR 新 paneId，仍 3 个 pane。
8. `18-1440-replay-live.png`（1440x950，深色，`--replay-events`）：live bar 追加，EMA 68,315.13、RSI 34.77、ATR 69.87，状态栏“已推送 10 根”；证明 WS bar 触发后 legend/副图更新。

早期探索截图 `00`–`06` 仅用于视口/交互排障，不作为验收证据。

### 已知限制

- 未实现 `WS subscribe_indicators` 的指标推送字面语义；采用任务单允许的降级路径：WS bar 触发 550ms 去抖，再带 `live=1` 调 GET（默认参数）或 POST（非默认参数），由后端 `recent_bars` 追加尾段并在 Python 重算。前端不做指标数学。
- TA-Lib/talipp 尚未接入运行时适配器；当前唯一权威是纯 Python 算子核，二者及黄金向量作为后续交叉校验与回归基准。

## Lead 验收

2026-11-19 合并 `v1-analysis-workbench` 任务单 04，合并提交 `8d80990 Merge branch 'ticket-04-indicators'`（分支末提交 `bafc47c`）。

- 合并后主分支验证：`uv run pytest -q` → 185 passed, 1 deselected（仅 Starlette/httpx 弃用 warning）；`uv run talaria contract-check` → 零漂移；`pnpm -C dsh typecheck` 通过；`pnpm -C dsh --filter @talaria/desk-ui test` → 42 passed（8 文件）；`pnpm -C dsh build` 通过（JS 490.62 kB / CSS 13.55 kB）。
- 审查确认：主图指标显式 `paneId:"candle_pane"` 且 `isStack=true`，EMA/SMA/BOLL/VWAP 共用主图；RSI/ATR 各自副图，`overrideIndicator` 复用 paneId；`calc` 只消费后端序列，`Number` 仅在 `indicatorData.ts` 的 Canvas 最后一跳；`live=1` 由 `FeedSupervisor.recent_bars` 追加快照末尾之后的实时尾段，Python 重算，前端不保留指标数学。
- 截图已复制到主仓库 `.scratch/tasks/ticket-04/`（`11`–`18` 共 8 张验收图，早期 `00`–`06` 为排障记录）。
- 接受已知限制：`subscribe_indicators` 的字面 WS 推送未实现，按允许的降级路径（WS bar 触发去抖查询 + Python 尾段重算）交付；TA-Lib/talipp 依赖已锁定并登记许可，暂未接运行时适配器，纯 Python 算子核为唯一权威。
