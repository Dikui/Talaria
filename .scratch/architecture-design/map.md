# Talaria 系统架构设计决策地图

ADR review: done 2026-11-18
Labels: wayfinder:map
Status: resolved

## Destination

产出一份完整、可检验的 Talaria 系统分层架构与模块接口规范文档（定稿 `docs/architecture.md` 并同步校准 `GLOSSARY.md` 与 `docs/adr/`），明确加密永续与美股日内交易下“人工 + Agent 协同互补双轨分析”全链路各层职责、接口契约、数据模型与单机部署拓扑。

## Direction

选定方向：自建 Python 标准分层底座 + 精选轻量开源库（`DuckDB/Parquet/SQLite`、`Optuna`、`KLineChart v10`）+ DeepSeek Harness 双端插件（类别：自建）；不保留多个方向。

## Notes

- **业务领域与范围**：面向加密货币（永续合约为主）与美股的分钟级及以上日内/波段交易，软件架构通过通用 `Instrument` 与 `Capability Traits`（`Market Pack`）保持跨品种、跨场所可扩展；现阶段以高保真模拟为主，在执行层预留标准实盘替换接口（`Execution Seam`）。
- **核心动机**：实现个人交易员与智能体的协同互补双轨分析（`Co-Pilot Analysis`）——人工可独立基于系统看盘、画线、设预警与下达模拟计划，智能体也可基于同一套时点数据底座扫描分析，且双方可围绕同一份图表标注与交易方案双向交互、协同修正。
- **可检验成功标准**：架构规范无遗漏地覆盖并打通以下全部环节，且每层职责单一、依赖单向、新增市场无需跨多层修改核心代码：① 多市场行情/衍生品费率/事件数据存储；② `Watchlist` 自选观察池与价格/指标/画线 `Alert` 预警；③ 宏观与财报 `Catalyst Calendar` 事件日历；④ 异动选标的扫描；⑤ 基于 `KLineChart` 的人机共享画线看盘界面；⑥ 纯量化策略与 DeepSeek Harness 智能体策略；⑦ 基于 `Optuna` 的防过拟合参数回测寻优；⑧ 多独立虚拟子账户组合定仓与硬风控；⑨ 含决策延迟墙与四重屏障的高保真模拟执行（预留实盘卡槽）；⑩ 支持 `HUMAN / CO_PILOT / AGENT` 来源归因的多子账户复式账本与绑定图表快照、`Trajectory` 思考链的 `Trade Journal` 复盘面板。
- **反目标（技术跑通仍算失败的情况）**：① 慢速大模型推理阻塞人工手动看盘、画线、切图或下达模拟计划；② 模块耦合紧，修改单一策略、指标或新增一个市场需要跨多层改动主干；③ 本地运行依赖过多重型中间件，单机日常启停与维护负担过重。
- **已验证选型前置证据**：工作台、图表画线与寻优开源底座核查见本地 `docs/research/copilot-trading-workbench-sources.md`（排除 `GPL-3.0` 的 `Freqtrade/FreqUI` 与 `PolyForm Noncommercial` 的 `zhu1090093659/dsh-trading` 源码复制）。
- **使用技能**：`/grilling`、`/domain-modeling`、`/codebase-design`；需查外部第三方接口时用 `/research`，需验证交互或状态机形态时用 `/prototype`。
- **实际交付说明**：本次规划包含最终文档交付——待全部决策任务单解决并通过交接前复查后，汇总定稿写入仓库 `docs/architecture.md`、`GLOSSARY.md` 与 `docs/adr/`（不包含编写业务实现代码）。

## Decisions so far

- [核查加密永续公开衍生品流与美股宏观事件日历 API 能力边界](./issues/02-perp-and-calendar-data-sources-research.md) — 查实 Bybit V5 免密钥覆盖全历史衍生品流、Binance 需配官方归档补 30 天外历史 OI、Hyperliquid REST K 线限 5000 根；美股与宏观事件日历采用 Finnhub 预期排期 + SEC EDGAR 8-K 秒级受理时间 + BLS/BEA/FOMC 官方时刻表与 ALFRED 修订库组合，详情见链接。
- [确立全局软件分层、模块边界与双端通信协议](./issues/01-software-layers-and-ipc-boundary.md) — 定型“1 底座 + 5 单向核心业务层 + 2 外圈编排与交互层”无环架构；开仓与放宽止损统一过 L3 组合风控，收紧止损与减平仓直达 L4；看盘前端拆为独立 React 包配薄 DSH 插槽壳经本地快通道直连 Python，智能体拆为 `talaria-desk`（仅写暂存草案）与 `deepseek-harness-sdk` 托管的 `talaria-auto/eval` 双隔离平面，详情见链接。
- [设计数据层、事件日历与盘面预警感知模块接口](./issues/03-data-calendar-and-alert-layer.md) — 定义含供流窗口与心跳语义的 `FeedCapabilityTrait`、实时热端冻结快照即时物化的 `PerceptionStore`、`CatalystEvent` 按精度等级保守防前视折算与历史排期覆盖声明，以及面向统一 `MarketEventFeed` 的 `Watchlist / Alert / Scanner` 双轨路由契约，详情见链接。
- [设计人机共享图表画线状态与双轨协同交互协议](./issues/04-shared-chart-and-copilot-interaction.md) — 基于 `KLineChart v10` 核心库将图元物理隔离为人工画线、经 ATR 包络与溯源硬门控校验的智能体标注、实时单向投影的执行屏障三层；确立框选会诊包、`StagedPlanDraft` 拖拽预试算确认流转及互斥的 `HUMAN / CO_PILOT / AGENT` 归因准则，详情见链接。
- [设计统一策略层接口与防过拟合参数寻优架构](./issues/05-strategy-and-optuna-optimization-layer.md) — 统一自动策略、持久化 `ControllerPatch` 与人工/协同草案出口契约，在底座沉淀基于紧凑交易序列与断流重预热的三端同口径算子库；外圈集成 `Optuna`（MIT）寻优器，强制前置双审计门禁、三段封存切分、全试验留痕及邻域平坦度与跨 `feed_id` 晋升校验，详情见链接。
- [设计多子账户组合风控层与带实盘预留位的模拟执行层](./issues/06-subaccount-guard-and-sim-execution.md) — 确立虚拟子账户单标的单一仓位回合（`Position Round`）不变量、七步确定性风控流水线与盘中回撤硬熔断守护；`Execution Seam` 区分直通收紧与受控放宽接口，由外圈投喂只读保证金估值视图支撑全仓强平与六条诚实仿真法则，详情见链接。
- [设计多子账户复式账本、全成本归因与交易复盘日志模块](./issues/07-multi-account-ledger-and-trade-journal.md) — 采用按子账户与币种物理隔离的十进制仅追加复式账本并纳管子账户级全量认知 Token 成本流水；实现六维全成本归因与隔离反事实沙箱下的 `Co-Pilot Lift` 评测；`Trade Journal` 永久绑定冰封画线快照、热端物化快照与 DSH `Trajectory` 指针，详情见链接。
- [确定单机轻量化进程拓扑、运行态配置与数据库分工](./issues/08-local-deployment-and-runtime-profiles.md) — 定型零重型中间件的本地守护 + 可选 Web 协同 + `stdio` 自治/回放 + 内存隔离多进程寻优运行态；按进程内唯一写者拆分为 5 个正交 SQLite 库配 `JournalStorage` 根除写锁争用，并固化断流/超时降级与五项跨缝隙自动化契约门禁，详情见链接。

## Not yet specified

## Out of scope

- 真实券商与交易所实盘下单、预写事务日志恢复及外部柜台对账实现 — 现阶段聚焦高保真模拟验证，仅在执行层预留标准替换接口位置。
- 微秒/毫秒级订单簿（L2/L3）高频做市与跨交易所低延迟套利 — 超出日内分钟级及以上双轨协同交易定位。
- 多用户账号体系、多租户权限隔离、计费与对外商业 SaaS 服务 — 仅面向个人单用户本地/私有环境使用。

## Global checks

- 交接前（覆盖已解决 5、8 张）：独立子代理第 1 轮复查发现冲突 4 项、风险与缺口 7 项；已按推荐方案全部闭环解决——消除放宽止损 L4→L3 反向依赖、确立单子账户单标的单一仓位回合不变量、校准 5 个单写者 SQLite 库与实时热流快照即时物化、隔离 `talaria-desk` 与 `talaria-auto` 双智能体宿主权限、补齐子账户全量认知 Token 成本台账、`ALPACA_IEX` 供流窗口收窄与紧凑交易柱算子对齐规则，并同步修订 `GLOSSARY.md` 与 `ADR-0001..0007`；改写的任务单：[确立全局软件分层、模块边界与双端通信协议](./issues/01-software-layers-and-ipc-boundary.md)、[设计数据层、事件日历与盘面预警感知模块接口](./issues/03-data-calendar-and-alert-layer.md)、[设计人机共享图表画线状态与双轨协同交互协议](./issues/04-shared-chart-and-copilot-interaction.md)、[设计统一策略层接口与防过拟合参数寻优架构](./issues/05-strategy-and-optuna-optimization-layer.md)、[设计多子账户组合风控层与带实盘预留位的模拟执行层](./issues/06-subaccount-guard-and-sim-execution.md)、[设计多子账户复式账本、全成本归因与交易复盘日志模块](./issues/07-multi-account-ledger-and-trade-journal.md)、[确定单机轻量化进程拓扑、运行态配置与数据库分工](./issues/08-local-deployment-and-runtime-profiles.md)；筛选新增 `ADR-0005`、`ADR-0006`、`ADR-0007` 并修订 `ADR-0001..0003`。
- 交接前第 2 次终审复查（已解决 8 张）：独立子代理核验冲突 0 项、风险与缺口 0 项、未提交路径违规链接 0 项；地图与交付文档全部一致，准予交接。
