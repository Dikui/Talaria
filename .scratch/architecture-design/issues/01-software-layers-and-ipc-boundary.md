# 确立全局软件分层、模块边界与双端通信协议

Labels: wayfinder:grilling
Type: grilling
Status: resolved
Blocked by: none

## Question

面对“人机协同双轨分析 + 全功能日内模拟交易”的范围，全系统最终应划分为哪几个标准软件分层（例如底座契约层、数据与感知层、策略与寻优层、组合与风控层、执行与仿真层、账本与复盘层、工作台交互层）？各层的精确职责边界、单向调用与事件流规则、单仓库目录结构，以及 Python 后端与 DeepSeek Harness 插件宿主之间的跨进程通信分工（实时行情/预警推送 vs 人工低延迟操作 vs 智能体工具调用）应如何定型，才能保证人工操作零大模型阻塞且各层高内聚？

## Answer

按用户确认全部采用推荐方案（Q1–Q5 均选定选项 A），全局软件分层、模块边界、目录结构与跨进程通信分工定型如下：

### 1. 全局软件分层（`1 公共底座 + 5 单向核心业务层 + 2 外圈编排与交互层`）

核心业务流水线严格保持自上而下、单向无环；需要串联全链路的回测寻优与需要跨层发起交互的人机工作台置于核心五层外圈，只允许外圈单向调用核心层，核心五层绝不反向依赖外圈：

1. **L0 公共底座契约层（`foundation`）**：
   - 统一高精度定点 `Decimal`、五重时点（`ts_event`、`ts_published`、`ts_available` + `PrecisionTier`、`ts_ingested`、`ts_committed`）、`Instrument` 与能力特征接口（`MarginTrait`、`CarryTrait`、`SessionTrait`、`OrderConstraintTrait`、`FeedCapabilityTrait`）、全系统跨层不可变契约对象，以及零框架依赖的确定性技术与衍生品算子核（`operators`，前导 `null` 等长对齐数组，供感知层预警、策略层与前端/PTC 同口径复用）。
2. **L1 数据与盘面感知层（`perception`）**：
   - 封装多市场数据源 `connectors`、冷热一体时点存储 `pit_store`、`Catalyst Calendar` 事件日历，并作为唯一事实源持久化托管 `Watchlist`、`Alert` 规则与人机共享图表画线 `OverlayRecord`；运行向量化 `Scanner` 与毫秒级 `AlertEngine`。
   - 向下游唯一交付：`EvidenceSnapshot`（含当时可见行情、衍生品状态、事件日历与图表有效画线）与 `TriggerEvent`。
3. **L2 策略与协同草案层（`strategy`）**：
   - 暴露统一 `Strategy` 接口与声明式参数空间 `ParamSpace`，容纳纯代码量化策略（`quant_strategies`）、DeepSeek Harness 智能体策略运行器（`agent_strategies`，支持 `SetupInsight` 与有界 `ControllerPatch` 双模式）及人工/人机协同草案装配器（`plan_drafter`，管理 `StagedPlanDraft` 并标记决策来源 `DecisionOrigin = HUMAN | CO_PILOT | AGENT`）。不感知子账户资金规模，不直接生成委托单。
4. **L3 多子账户组合与风控层（`portfolio_risk`）**：
   - **全系统唯一开仓与加仓关口**（`HUMAN`、`CO_PILOT`、`AGENT` 三类来源的新开仓/加仓一律经过本层，未提供逻辑证伪止损价 `invalidation_price` 一律拒单）。消费只读 `AccountState`，按子账户隔离执行 `Risk Unit (R)` 定仓（及手动指定数量的隐含风险封顶校验）、相关性簇风险预算、时段/PDT/事件静默规则、衍生品 `Mark Price` 强平安全垫与扣除全摩擦净赔率硬门禁。
   - 向下游交付：`ApprovedExecutionPlan` 或带明确拒绝码的 `RejectedPlanRecord`。
5. **L4 执行与高保真仿真层（`execution`）**：
   - 对外暴露极简深接口 `ExecutionEngine`（`Execution Seam`），内置自治 `PositionExecutor` 四重保护屏障状态机，后接高保真 `SimExecutionAdapter` 与未来实盘预留位 `LiveExecutionAdapter`。
   - **人工紧急避险直通旁路**：人工主动平仓、减仓、撤单、收紧止损线及切换 `TradingState`（`REDUCING` / `HALTED`）允许绕过策略与定仓层直达本层毫秒级执行（放宽止损仍须经 L3 强平安全垫与风险上限校验）。
6. **L5 复式账本与复盘层（`ledger_journal`）**：
   - 独占维护多子账户精确十进制复式账本 `Ledger`（向 L3 提供唯一合法闭环只读投影 `AccountState`）、全成本与延迟损耗归因引擎 `Evaluator`，以及关联成交、图表画线快照、人工笔记与 DSH `Trajectory` 指针的 `TradeJournal`。
7. **O1 外圈运行与寻优编排层（`orchestration`）**：
   - 包含实时守护编排器 `ShadowCoordinator`、历史回放器 `ReplayRunner` 及基于 `Optuna` 的防过拟合寻优引擎 `StudyOptimizer`（串联前置前视/预热审计与隔离内存账本驱动 L1→L5 循环，避免策略层反向耦合执行与账本）。
8. **O2 外圈双端工作台交互层（`workbench`）**：
   - 包含 Python 本地回环快通道网关（HTTP REST + WebSocket）、零 DSH 耦合的纯 React 看盘组件包（集成 `KLineChart v10`）及薄 DeepSeek Harness 插件适配壳（挂载 `ui-slots` / `typert`），支持同屏双轨协同与无 DSH 独立看盘两种打开方式。

### 2. 横向市场扩展机制与单仓库目录结构

跨品种差异继续通过横向 `Market Pack`（`packs/crypto_perp/` 与 `packs/us_equity/`）向 L0/L1/L2 注册特征、连接器与领域 `Skill`，核心五层禁止编写资产类别分支。单仓库目录定型如下：

```text
Talaria/
├── GLOSSARY.md
├── contracts/                          # Python 自动导出的跨语言 JSON Schema 快照与算子黄金测试向量
├── configs/                            # 运行态 Profile、子账户模板、风控与仿真摩擦配置
├── core/                               # Python 确定性底座（uv 管理）
│   ├── pyproject.toml
│   └── src/talaria/
│       ├── foundation/                 # 【L0】Decimal、五重时间戳、Instrument+Traits、契约对象、纯函数算子核
│       ├── perception/                 # 【L1】connectors、pit_store、catalyst_calendar、watchlist、overlays、alert_scanner
│       ├── strategy/                   # 【L2】Strategy 接口+ParamSpace、quant_strategies、dsh_agent_runner、plan_drafter
│       ├── portfolio_risk/             # 【L3】subaccount_book、r_unit_sizer、correlation_budget、hard_risk_guard
│       ├── execution/                  # 【L4】ExecutionEngine Seam、position_executor、sim_broker、live_gateway_stub
│       ├── ledger_journal/             # 【L5】double_entry_ledger、cost_evaluator、trade_journal
│       ├── orchestration/              # 【O1】shadow_coordinator、replay_runner、study_optimizer (Optuna)
│       ├── workbench_api/              # 【O2-后端】127.0.0.1 HTTP/WS 快通道网关与静态独立看盘页托管
│       ├── packs/                      # 【横向市场规则包】crypto_perp / us_equity
│       └── cli/                        # 统一命令入口（up / replay / optimize / audit）
├── dsh/                                # 前端与 DeepSeek Harness 插件工程（pnpm workspace）
│   ├── package.json
│   ├── pnpm-workspace.yaml
│   └── packages/
│       ├── talaria-contracts/          # 由 contracts/ 自动生成的 TS 类型与 DSH 规约 Schema
│       ├── talaria-desk-ui/            # 【O2-前端核心】零 DSH 依赖的 React + KLineChart v10 看盘/画线/下单/复盘组件包
│       ├── dsh-bridge/                 # DSH 宿主与 Python 底座本地通信桥、单调执行守卫
│       ├── dsh-strategy-agents/        # 【归属 L2】AGENTS.md 前缀、PTC 同口径算子绑定、市场分析 Skills
│       └── dsh-workbench/              # 【O2-插件壳】talaria-desk / talaria-eval Profile 与 ui-slots / typert 适配层
├── tests/                              # 与分层同构的接口契约测试、算子对齐测试与仿真账本回归测试
└── docs/
    ├── architecture.md
    └── adr/
```

### 3. 双端三通道跨进程通信分工

1. **实时行情与状态推送快通道（Python `workbench_api` → Browser WebSocket，`127.0.0.1`）**：毫秒级直推 K 线/BBO/`Mark Price` 增量、活跃四重屏障位移、`Alert` 触发提醒、`StagedPlanDraft` 更新与子账户净值变化，不经 DSH Node 宿主二次中转。
2. **人工低延迟交互快通道（Browser → Python `workbench_api` HTTP REST，`127.0.0.1`）**：切图加载、画线增删改（前端拖拽乐观渲染、松手落库）、自选与预警管理、协同草案确认、手动开仓提交及紧急撤单/平仓直连 Python，物理隔离大模型推理队列，确保人工操作零阻塞。
3. **智能体认知与回测通道（DSH Host ↔ Python）**：
   - 实时 `Shadow Run`：`dsh-bridge` 通过 `defineTool` 调用本地 Python 受控只读快照接口与草案提交接口；Python `ShadowCoordinator` 将开启了智能体唤醒的 `TriggerEvent`（受冷却时间与并发度约束）异步推送至 DSH 会话；对话流、`Trajectory` 展示与单次审批走 DSH 原生 `typert` 通道。
   - 历史回测：Python `ReplayRunner` 通过官方 `deepseek-harness-sdk`（`stdio` JSON-RPC 2.0）驱动物理移除 `shell`、外网与文件写权限的 `talaria-eval`（基于 `sdk-minimal`）子进程。
4. **跨语言单一契约源（代理已按既有 ADR 定案）**：以 Python `foundation` 模型为唯一真值源生成 JSON Schema（价格/金额跨 JSON 一律采用十进制字符串，仅前端 Canvas 绘制最后一跳转为浮点数），构建脚本自动剥离根级 `$schema` 并展开 `$defs` 以通过 DSH Schema 校验器。


## Comments

### 交接前整体复查修订（覆盖已解决 5、8 张）
- **原因**：独立复查指出三处边界需收紧：① 人工放宽止损若由 L4 回调 L3 会破坏单向依赖；② 实时态下自治子账户若依赖未核实的 `dsh web` 后台多会话接口存在返工与越权风险；③ L4 全仓强平仿真需合法获取只读权益视图。
- **修订结论**：
  1. **严格单向风险变更路由**：收紧止损、移至保本、撤单与减平仓直达 L4（L4 硬断言风险只减不增）；放宽止损一律走正向 `O2 → L3.approve_barrier_widening → L4.apply_approved_amendment`，杜绝 `L4 → L3` 反向依赖。
  2. **智能体双宿主物理隔离**：`dsh web --profile talaria-desk` 专职人机协同（物理仅挂载只读工具与 `stage_plan_draft`，无自动直下计划权限）；智能体自治子账户统一由 `talaria-daemon` 经官方已核实的 `deepseek-harness-sdk`（`stdio` JSON-RPC 2.0）驱动受限 `talaria-auto` Profile，实现与历史回测 `talaria-eval` 100% 路径同构及物理权限隔离。
  3. **只读保证金估值投喂**：由外圈 O1 编排器将源自 L5 的只读 `MarginValuationView` 显式传入 L4 `SimExecutionAdapter` 步进函数，保持核心五层零反向导入。
