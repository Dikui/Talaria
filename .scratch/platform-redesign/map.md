# platform-redesign：按九类拆分的总览

整体需求见 [spec.md](spec.md)，目标架构见 `docs/architecture.md`。2026-11-21 起，工作按下面九类分开讨论、分开交付。每类一个目录 `.scratch/<分类>/`，里面有自己的任务单，编号从 01 开始。引用其他类的任务单写成 `<分类>/NN`，比如 `data-access/04`。没有任务单的类，等第一张单出现时再建目录。

## 九类

| 类 | 目录 | 管什么 | 任务单 | 待新开 | 以后 |
|---|---|---|---|---|---|
| 市场接入 | `market-access/` | 数据源与市场包、实时连接与断流处理、事件日历、快讯 | — | — | 美股市场包、事件日历、快讯、其他交易所；回放模式拒绝使用日常数据目录 |
| 数据存取 | `data-access/` | 冷库、取数 SDK、范围取数、订阅语义、逐笔与盘口、衍生品入库 | 01–08 | — | 存储容量预算 |
| 平台 | `platform/` | 常驻服务与启停、功能开关、DSH 功能面板与兜底页面、存储与锁、契约导出 | 01–02 | `talaria open` 入口命令；服务托管 DSH 子进程 | Tauri 薄壳 |
| 图表 | `charting/` | K 线显示、画线工具、撤销重做、快捷键、页面联动 | — | 借鉴清单第 1 批（对象树、撤销重做扩到指标、快捷键与命令面板、磁吸、锁定与隐藏）；第 2 批的常用画线工具 | 其余画线工具 |
| 分析 | `analysis/` | 指标算子、关键位引擎、组件模型 | 01–03 | 第 2 批指标（MACD、Stochastic）与指标模板；第 3 批（输入 Schema、图形输出） | — |
| 预警与扫描 | `alerts-and-scanning/` | 规则、预警、扫描、简报 | 01–02 | 扫描改为可开关功能并规则化 | — |
| AI | `ai/` | 工具接口、agent 启动入口、AI 场景 | — | 关键位复核（工具、会诊、简报留在 DSH 插件，见 ADR-0020） | 其他 AI 场景 |
| 交易 | `trading/` | 账户进程、定仓与风控、模拟成交、模拟盘 | 01–06 | — | 实盘成交插件、`ControllerPatch` |
| 回测与研究 | `backtest-and-research/` | 回放、寻优、挖因子、agent 评估、复盘与归因 | 01 | — | 寻优、挖因子（见 `docs/research/llm-alpha-mining.md`）、agent 评估、复盘与归因、认知成本入账 |

`foundation`（契约、Decimal、时点）不单独成类，修改时归到发起修改的那一类。

## 现在可以开工的任务单

不依赖任何未完成的任务单：

- `data-access/01`：冷库数值格式迁移到 decimal128
- `platform/01`：launchd 常驻服务与功能开关
- `platform/02`：功能组件挂进 DSH 面板，兜底页面复用（2026-11-22 按 ADR-0020 改写）
- `analysis/01`：关键位注册表
- `analysis/03`：EMA、ATR、VWAP 对齐 TradingView 与默认参数

## 开工顺序（2026-11-21 定）

按“依赖 + 是否改同一批文件”分批，同批并行，各开一个 worktree：

| 批 | 任务单 | 说明 |
|---|---|---|
| 1 | `data-access/01`、`platform/01`、`analysis/03` | 主干起点；先把服务装配与功能开关改完，后面的单往里接；指标口径尽早定下 |
| 2 | `data-access/02`、`data-access/03`、`platform/02` | 02 与 03 都改实时解析与热缓冲，先合 02，03 在其上合；`platform/02` 开工前先把原型结论写进任务单 |
| 3 | `data-access/04`、`analysis/01` | 04 完成即“一两行代码取数”可用；`analysis/01` 等 `data-access/02` 合并后再做 |
| 4+ | `analysis/02` → `alerts-and-scanning/01`、`trading/01` → 交易链 | `data-access/05`–`08` 在 04 之后插空，`data-access/08` 须早于 `trading/03` |

不能同批的原因（共同改动的位置）：

- 快照构建与冻结（`perception/snapshot_builder.py`）：`data-access/01`、`data-access/02`、`analysis/01`。
- 服务装配与启动（`workbench_api/runtime.py`、`service.py`）：`platform/01`、`data-access/02`、`analysis/01`。所以 `data-access/02` 排在 `platform/01` 之后，冷库写入器直接接上开关。
- 预警引擎（`perception/alerts/engine.py`）：`analysis/01`、`analysis/03`。
- 实时解析与热缓冲（`feed/`、`packs/crypto_perp/stream.py`）：`data-access/02`、`data-access/03`。

图表类第一张单在 `platform/02` 之后由同一条前端线接着做。

## 跨类依赖

- `analysis/02` 组件模型 ← `data-access/04` 取数 SDK
- `alerts-and-scanning/01` JSON 规则 ← `analysis/02`
- `alerts-and-scanning/02` 规则编辑器 ← `platform/02`
- `trading/01` 账户进程 ← `analysis/02`
- `trading/03` K 线模拟成交 ← `data-access/08` 衍生品入库
- `trading/04` 模拟盘 ← `data-access/06` 策略订阅
- `trading/05` 盘口加逐笔模拟成交 ← `data-access/07`、`backtest-and-research/01`
- `backtest-and-research/01` 回放 ← `trading/02`、`trading/03`
- AI 的关键位复核 ← `analysis/01`（待新开）

## 旧编号对照（2026-11-21 前的 platform-redesign/issues）

| 旧 | 新 | 旧 | 新 | 旧 | 新 |
|---|---|---|---|---|---|
| 01 | data-access/01 | 08 | analysis/01 | 15 | trading/02 |
| 02 | data-access/02 | 09 | platform/01 | 16 | trading/03 |
| 03 | data-access/03 | 10 | platform/02 | 17 | trading/04 |
| 04 | data-access/04 | 11 | analysis/02 | 18 | backtest-and-research/01 |
| 05 | data-access/05 | 12 | alerts-and-scanning/01 | 19 | trading/05 |
| 06 | data-access/06 | 13 | alerts-and-scanning/02 | 20 | data-access/08 |
| 07 | data-access/07 | 14 | trading/01 | 21 | trading/06 |

另外，`indicator-tv-alignment/01` 改为 `analysis/03`。

## 待定（来自 2026-11-21 的 agent 接入讨论，尚未写进 ADR）

- ~~DSH 不保留特殊地位，只是客户端之一；DSH 专用代码等替换完成后删除。~~ 2026-11-22 推翻，见 ADR-0020：DSH 是日常入口和 agent 宿主。
- 模型在各模块里只做判断和提议，不改写指标、关键位、风控结论；判断绑定快照和产生时刻，记录后不再重算。
- 第一个 AI 场景是关键位复核，结果先只展示，等用反应率检验有效后再用于过滤。
- 凡是受 AI 影响的产物（包括模型生成的公式），都按 ADR-0009 的证据等级评估。
