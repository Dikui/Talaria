# Talaria 人机协同交易工作台与寻优复盘底座：一手来源核查

核查日：**2026-11-18（Asia/Shanghai）**。定位：围绕「人机协同双轨看盘、图表画线、自选预警、事件日历、复盘面板与参数寻优」评估四个方向的开源候选底座，服务低频研究、历史回测、paper-first 阶段架构选型。

方法：仅核查官方 GitHub 仓库、源码路径与设计契约文档、官方文档、官方公告、许可证文本及 npm/PyPI 发布元数据；**未安装、未运行外部项目，未登录、未调用收费 API、未交易**。不使用二手营销解读。全文严格区分**已核实官方事实**与**架构推断 / Talaria 选型建议**；软件许可证分析系工程合规核查，不构成法律意见。

## 1. 核心结论

1. **`zhu1090093659/dsh-trading` 不可直接复制或合入商业/宽松开源代码库，只能做洁净室（clean-room）架构参考；且必须与同名生态插件 `maddogfinance/dsh-trading` 区分。** `zhu1090093659/dsh-trading` 当前许可证为 **PolyForm Noncommercial 1.0.0**（非商业源码可用许可，商业使用须事先书面授权）[D1]；而经常被混提的 `annotate_chart`（带 K 线窗口价格区间硬校验与数据溯源门控的图表标注工具）实为独立仓库 **`maddogfinance/dsh-trading`**（**MIT** 许可）的核心实现 [D6]。两者均基于 TradingView Lightweight Charts，均未内置面向人类的完整交互式画线工具栏，也不具备持久化条件预警引擎、PIT 事件日历与组合级参数寻优回测。
2. **双轨人机共绘 K 线底座优先选 `klinecharts/KLineChart`（v10，Apache-2.0）核心库 + 自研薄 React 工具栏，而非在 `lightweight-charts`（v5，Apache-2.0 + `NOTICE` 归属要求）上从零手写整套几何画线引擎。** Lightweight Charts `v5.3.0` 虽提供底层 `Series Primitives / Pane Primitives` 画布钩子，但官方明确不内置交互画线工具栏与技术指标公式，且强制要求保留 TradingView 公开归属声明与链接（默认开启图表 `attributionLogo`）[L2]、[L3]；`KLineChart` `v10.1.0` 原生内置 `Overlay` 几何画线系统与 20+ 主副图技术指标引擎，Apache-2.0 无强制品牌徽标条款 [K1]、[K2]，但需注意其官方成品工具栏封装 `@klinecharts/pro` 基于 **SolidJS** 构建，不宜直接硬塞入 React/DSH 宿主，应直接调用 `klinecharts` 核心 API 自建统一的人机共绘 Overlay 状态层 [K3]。
3. **OpenBB 的开源边界在 2026 年下半年发生重大变化：OpenBB Workspace 已不再是闭源 SaaS，而是以 Apache-2.0 开源并移交 FINOS Labs（`finos-labs/finos-workspace`），但它在工程形态上仍不适合直接嵌入 DeepSeek Harness。** 已核实官方事实是：随 OpenBB 结束商业化运营，OpenBB Platform `openbb-v5.0.0`（归入 OpenBQ Foundation）与 OpenBB Workspace（归入 Linux 基金会旗下 FINOS Labs）现均为 **Apache-2.0** 开源代码 [W1]–[W3]。但开源后的 `finos-workspace` 本质是一套自带 FastAPI + SQLAlchemy + RBAC + Docker 容器编排的重型独立全栈看板宿主，且核心仓剥离了商业表格/专业 K 线画线库（仅内置 Markdown、HTML、PDF、新闻与插件扩展协议），直接嵌入 DSH 会造成双宿主与双 Agent 协议冲突 [W3]、[W4]。
4. **参数寻优与 Web 复盘严禁源码级引入 `Freqtrade / FreqUI`（均为强传染性 `GPL-3.0`），应直接采用 `Optuna`（`v5.1.0`，`MIT`）作为寻优内核，并洁净室吸收 Freqtrade 的前视/递归诊断与样本外防过拟合纪律。** 直接 `import` 或裁剪改写 `freqtrade/optimize/hyperopt` 与 `freqtrade/frequi` 并在未来分发软件，将触发 GPL-3.0 传染义务 [F1]、[F4]；`Optuna` 与 `optuna-dashboard` 均为宽松 MIT 许可 [P1]、[P3]，但 Optuna 只是领域无关的黑盒采样器（`TPESampler`、`NSGAIIISampler`、`JournalStorage` 等），**不内置任何金融回测引擎、前视偏差检测、滚动样本外切分或多重检验过拟合校正**，若不加防线直接寻优极易把未来函数或历史噪声拟合成虚假高夏普曲线 [F2]、[F3]、[P2]。

## 2. 候选底座总览对比表

| 候选项目 / 仓库 | 核查版本 / 仓库快照与许可证 | 已核实核心能力 | 已核实边界与缺失能力 | Talaria 采用建议（架构推断） |
|---|---|---|---|---|
| **`zhu1090093659/dsh-trading`** | `main` 分支；**PolyForm Noncommercial 1.0.0**（非商业许可，非 OSI 开源）[D1] | 基于 DSH/Cordis 的三栏终端；`Tool × Registry/Store × View` + SSE 刷新；`@dshtrading/*` 多市场连接器（含金十 MCP 快讯/周历）、`TvChart.tsx`（Lightweight Charts v5）、`@dshtrading/watchlist`、单标的纯函数回测 `@dshtrading/strategies`、带 `staged` 与 `FIFO rounds` 的 `@dshtrading/holdings`；默认模拟与人工审批闸门 [D1]–[D5] | **禁止未授权商业使用/分发源码**；无人类交互画线工具栏；无持久化价格/指标条件预警引擎；日历仅为实时周历查询而非 PIT 事件库；回测仅限单标的基础滑点/手续费模拟，无组合资金池、参数寻优与前视检测 | **不复制任何源码**；仅在洁净室条件下参考其 `Tool × Store × View` 状态同步、人工审批闸门与 `staged` 待确认台账交互范式 |
| **`maddogfinance/dsh-trading`**（同名生态辨析） | `main` 分支；**MIT** [D6] | 面向 DSH 的投研工作台插件（`@dsh-trading/*`）；实现 `annotate_chart`（价位线、区间带、路径推演 + 多空情景表）、`get_chart_view` 视口回读及**强制数据溯源 + K 线窗口价格范围硬门控** [D6] | 仅聚焦投研看盘卡片与只读风控阻断（`risk-guard`），不含完整自选/台账/回测/人类画线工具栏 | **可直接复用/改写其 `annotate_chart` 价格范围校验门控与双向视口同步机制** |
| **`tradingview/lightweight-charts`** | npm 最新稳定版 **`v5.3.0`**（2026-11）；**Apache-2.0 + `NOTICE` 归属要求** [L1]、[L2] | 高性能 HTML5 Canvas 金融图表；v5 原生支持多窗格（Panes）、`Series Primitives` 与 `Pane Primitives` 低层扩展接口；体积轻、渲染快 [L2]、[L3] | **必须履行 TradingView 归属声明与外链要求**（默认渲染 `attributionLogo`）；**不内置技术指标计算引擎，也不内置开箱即用的完整交互画线工具栏**（`plugin-examples` 仅为示例级代码） [L2]、[L3] | 若仅做轻量只读行情卡片可选；**不建议作为重度“人机双轨交互画线 + 多副图指标”主画布**，否则需自研整套几何拾取、磁吸、编辑与撤销栈 |
| **`klinecharts/KLineChart`**（及 `@klinecharts/pro`） | 核心库 npm **`v10.1.0`**；**Apache-2.0**（无强制徽标条款）[K1]。官方封装 `klinecharts/pro` 亦为 **Apache-2.0** [K3] | 原生内置 **Overlay 画线引擎**（射线/线段/平行通道/价格通道/斐波那契/画笔/标注，`createOverlay`/`registerOverlay`）与 **20+ 主副图技术指标引擎**（`createIndicator`/多窗格堆叠）[K2] | 核心库只管 Canvas 与交互图元，不含外部工具栏 DOM；而官方带工具栏的 `@klinecharts/pro` 使用 **SolidJS** 编写 UI 外壳，且 v9→v10 存在 `setDataLoader` 等破坏性 API 升级 [K1]、[K3] | **推荐作为主 K 线底座**：直接依赖 `klinecharts` `v10.1.0` 核心包，在 Talaria/DSH 侧用 React 自建薄画线工具栏，统一绑定人类鼠标画线与 Agent 标注 JSON |
| **OpenBB Platform + OpenBB Workspace（`FINOS Workspace`）** | Platform Python **`openbb-v5.0.0`**（`openbq-org/OpenBB`）**Apache-2.0**；Workspace（`OpenBB-finance/workspace` → `finos-labs/finos-workspace`）**Apache-2.0** [W1]–[W3] | 2026 年下半年起前后端全面转为 Apache-2.0 开源；Platform 提供标准化 provider 扩展骨架；Workspace 提供 React 18 看板网格、`widgets.json` 参数联动协议、`plugin.json` 组件插件与 FastAPI/RBAC 后端 [W2]–[W4] | **不是可嵌入 DSH 的轻量前端组件**：自带完整 FastAPI + SQLAlchemy + Docker 多服务底座与独立 Copilot 协议；开源版核心仓**不含** `ag-grid-enterprise` 或 TradingView 商业图表库，无原生 K 线画线与回测复盘台 [W3]、[W4] | **不搬运其前端工作台外壳进入 DSH**；仅借鉴 `widgets.json` 跨面板参数联动思想，以及按需参考 Platform v5 的数据 provider 分层（见同目录数据模板调研） |
| **`freqtrade/freqtrade`（`Hyperopt` + `FreqUI`）** | **`2026.10`** 系列；主仓与 `freqtrade/frequi` 均为 **`GPL-3.0`** [F1]、[F4] | `Hyperopt` 基于 Optuna 封装参数空间与 `IHyperOptLoss`；配套 CLI **`lookahead-analysis`**（截断检测前视偏差）与 **`recursive-analysis`**（检测递归指标预热偏差）；`FreqUI` 基于 Vue 3 + Apache ECharts 展示回测交易信号 [F2]–[F4] | **强传染性 GPL-3.0**：源码合入、Python 模块导入或前端组件改写分发均受 GPL-3.0 约束；加密交易所模型强耦合；高维 `--spaces all` 极易样本内过拟合，且不自动强制 Walk-Forward 切分 [F1]–[F3] | **严禁引入或改写任何源码**；在洁净室下借鉴其“先过前视/递归截断门禁，再跑受限空间样本外寻优”的工程流程 |
| **`optuna/optuna`**（及 `optuna-dashboard`） | `optuna` **`v5.1.0`**，**MIT**；`optuna-dashboard` **MIT** [P1]、[P3] | Define-by-run 动态参数空间；单目标/多目标采样（`TPESampler`/MOTPE、`NSGAIIISampler`、`GPSampler`、`QMCSampler`）、约束支持、`JournalStorage` / `RDBStorage` 多进程持久化、Web 寻优分析面板 [P2]、[P3] | **纯数学黑盒优化器，零金融领域护栏**：不含回测撮合、不含时序 Walk-Forward / Purged CV 切分器、不含前视检测与多重检验统计修正（如 DSR/PBO）；多目标模式下原生 `trial.report()` 剪枝抛出 `NotImplementedError` [P2] | **推荐直接作为参数寻优底层依赖**；必须由 Talaria 在外层包裹快照绑定、Walk-Forward 样本外隔离、前视截断审计与稳健目标函数 |

---

## 3. 分项一手核查与架构推断

### 3.1 `zhu1090093659/dsh-trading`（及同名生态插件辨析）

#### 已核实官方事实

1. **许可证红线（非商业源码可用，非开源）**：
   - `zhu1090093659/dsh-trading` 根目录 `LICENSE` 与 `README.md` 明确采用 **PolyForm Noncommercial License 1.0.0**。该许可仅免费允许个人、教育与研究用途，任何商业用途均须事先获得书面授权 [D1]。
   - **重名仓库辨析**：GitHub 上另有一个面向 DeepSeek Harness 的独立仓库 **`maddogfinance/dsh-trading`**，许可证为 **MIT**，包命名空间使用带连字符的 `@dsh-trading/*`（如 `@dsh-trading/client-chart`），定位为纯投研只读看盘工作台插件（内置 `risk-guard` 拦截下单工具）[D6]。两仓不可混为一谈。
2. **包结构与三元架构契约（`@dshtrading/*`）**：
   - `zhu1090093659/dsh-trading` 基于 DeepSeek Harness（底层微内核 Cordis）采用 `packages/` monorepo 组织，其 `docs/design/agentic-native-architecture.md` 定义核心三元组 **`Tool × Registry/Store × View`**：Host 侧 Store 作为唯一事实源（SSOT），Agent 调用 Tool 修改状态后通过 SSE（`invalidates`）实时驱动前端 View 刷新 [D2]。
   - 核心与安全包：`packages/base`（`@dshtrading/base`，账户/订单/报价核心契约、三栏 GUI 外壳、默认 `dry-run` 模拟与每笔实盘强制人工交互审批，无头会话 fail-closed）、`packages/authority`（Ed25519 实盘解锁签名平面）、`packages/bot-api` [D1]、[D2]。
   - 市场与连接器包：`@dshtrading/crypto`、`@dshtrading/us`、`@dshtrading/cn`、`@dshtrading/hk`、`packages/global` 及 19+ 个 `packages/connector-*`（均采用本地 BYOK 密钥，不二次分发数据）[D1]、[D2]。
   - 业务功能包：`packages/indicators`（零 DOM 依赖纯数学指标库）、`packages/watchlist`（`@dshtrading/watchlist`）、`packages/strategies`（`@dshtrading/strategies`）、`packages/holdings`（`@dshtrading/holdings`）及多个 `packages/client-ui-*` 视图包 [D2]–[D5]。
3. **K 线组件选型与 `annotate_chart` / 图表标注能力**：
   - `zhu1090093659/dsh-trading` 中栏主图组件 `TvChart.tsx` 采用 **TradingView Lightweight Charts v5**，通过「Vanilla 图表实例 + React `useRef` 外壳」封装（该模式在 `docs/design/knowledge-graph.md` 中被明确列为仓内可视化标准先例），支持 5m–周线多周期切换、主副图技术指标、自定义脚本指标（如 SuperTrend、TD9）、策略信号买卖点标记与知识图谱事件图钉 [D2]、[D3]。
   - 具名的 **`annotate_chart`**（由大模型直接在主栏实时图表上绘制支撑/阻力水平位、区间带与推演路径，同时在对话区输出多空情景价位表）、**`render_chart`**（导出静态图片）与 **`get_chart_view`**（将前端当前图表视口状态回传给 Host 供 Agent 读取），核实位于 MIT 许可的 **`maddogfinance/dsh-trading`** 仓内；该实现强制执行**证据溯源（provenance）**与**真实 K 线窗口价格区间硬校验门控**，越界幻觉价格会被直接拒绝 [D6]。
   - **缺失核实**：上述两个仓库均未实现供人类交易员使用的完整交互式画线工具栏（无鼠标自由绘制趋势线/斐波那契/通道、控制点拖拽微调、样式弹窗、撤销重做及人机同一图元双向编辑能力）。
4. **Watchlist / 预警 / 新闻与事件日历覆盖情况**：
   - **自选股（Watchlist）**：`@dshtrading/watchlist` 提供本地持久化（`~/.dsh/watchlists.json`）、跨市场分组、选中态同步与 `watchlist_list/add/remove/select` 工具 [D2]。
   - **快讯与宏观日历**：除各市场新闻工具（`us_get_news`、`cn_get_news`、`hk_get_news` 等）外，`packages/connector-jin10`（`@dshtrading/connector-jin10`）通过 MCP 接入金十数据（需用户自备 `JIN10_MCP_TOKEN`），注册 `flash_list`、`flash_search`、`econ_calendar`（本周宏观财经日历：日程、预期值、公布值）及 `news_list/search/get` 等 9 个只读工具 [D5]。
   - **缺失核实**：仓内**无独立的持久化价格/指标/画线突破条件预警引擎**（无后台规则状态机、冷却去重与触发审计记录）；`econ_calendar` 仅为滚动当周宏观日历查询，不含个股财报/除权分红/停复牌的 **PIT（Point-in-Time）历史事件日历数据库**，无法与历史回测时间轴无前视联动。
5. **Strategy Tab 回测实现边界（`docs/design/strategy-tab.md` & `packages/strategies`）**：
   - 定义纯函数式、浏览器端/Node 双端可运行的**单标的（Single-symbol）**历史 K 线回测引擎契约（`strategy_backtest`），内置唐奇安突破、RSI 回归、EMA 双均线、布林带回归、200 日均线与 12 月动量 6 个经典参考范式，支持基础手续费/滑点参数、自定义策略本地存储（`~/.dsh/strategies/custom.json`）、Node VM 沙箱与双端超时熔断，以及选股器扫描超时护栏 [D3]。
   - **边界核实**：不具备多标的共享现金池的组合级回测（Portfolio Backtest）、无 PIT 数据快照与历史退市标的池治理、无自动化超参数寻优（Hyperopt）或 Walk-Forward 样本外评估闭环、无内置未来函数检测工具。
6. **Holdings 台账边界（`docs/design/holdings-ledger.md` & `@dshtrading/holdings`）**：
   - 定义跨实盘、模拟盘与外部截图解析导入的统一资产台账契约，核心包含**待确认暂存区（`staged` / `holdings_stage` → 确认后入账 `holdings_list`）**、基于先进先出的**净持仓回合（`FIFO rounds`）**盈亏核算，以及多币种汇率换算契约 [D4]。
   - **边界核实**：定位是供人机对话对齐全局持仓上下文的运营级回合台账，而非与回测引擎同构的不可变复式资金/持仓审计账本（未见完整的历史除权除息/送配股自动复权账务引擎、非 FIFO 税盾批次核算，或绑定数据快照 ID 的逐笔归因复盘报告）。

#### 架构推断与可借鉴边界

- **合规隔离**：鉴于 `zhu1090093659/dsh-trading` 采用 PolyForm Noncommercial 1.0.0，Talaria **不应复制、改写或打包其任何源码文件**。其公开设计文档体现的三项高层交互理念——`Tool × Store × View` 单一事实源推送、外部导入/Agent 建议先入 `staged` 待人工确认后再落账、执行层默认模拟与人工审批闸门——属于通用软件架构模式，可在洁净室原则下自行设计实现。
- **可直接吸收的开源机制**：`maddogfinance/dsh-trading`（MIT）中的 **`annotate_chart` 真实 K 线极值门控 + 数据溯源字段校验 + `get_chart_view` 视口状态同步**，直接命中大模型看盘“瞎编支撑阻力位”的核心痛点，可在保留 MIT 版权声明前提下参考或自研同类校验器。

---

### 3.2 开源 K 线与人机画线组件对比：`tradingview/lightweight-charts` vs `klinecharts/KLineChart`

#### 已核实官方事实

1. **`tradingview/lightweight-charts`**：
   - **版本与仓库**：当前主版本为 **v5**，npm 最新稳定发行为 **`v5.3.0`**（2026 年 11 月发布）[L1]。
   - **许可证与归属徽标条款**：根目录 `LICENSE` 为 **Apache-2.0**，但同时附有具备约束力的 **`NOTICE`** 文件及 README 归属条款：使用者必须标明 TradingView 为产品创建者，在面向用户的公开页面展示 `NOTICE` 中的归属声明及指向 `https://www.tradingview.com/` 的链接；v5 图表配置项 `attributionLogo` 默认开启（`true`），在画布内渲染带链接的 TradingView 徽标（仅当已在公开页面满足上述归属与外链要求时，方可通过配置隐藏图表内徽标）[L2]。
   - **Primitives 插件机制与画线边界**：v5 原生支持多窗格（Panes）布局，并提供 `Series Primitives`（`ISeriesApi.attachPrimitive`）与 `Pane Primitives`（`IPaneApi.attachPrimitive`）三段式扩展架构（`Primitive` 状态 → `View` 逻辑时间/价格转像素坐标 → `Renderer` 原生 Canvas2D 绘制）[L3]。**关键事实**：官方文档明确指出 Lightweight Charts **不内置开箱即用的交互式画线工具集**（如趋势线、斐波那契回撤、平行通道、矩形、文本标注工具栏），也**不内置任何技术指标数学计算函数**；官方 `plugin-examples` 目录与 `npm create lwc-plugin@latest` 仅提供开发者自建插件的参考脚手架，鼠标事件捕获、锚点拖拽命中检测（Hit-testing）、磁吸、Z 序管理、样式编辑与撤销栈均须调用方自行研发维护 [L3]。
2. **`klinecharts/KLineChart`（及衍生生态 `@klinecharts/pro`）**：
   - **版本与仓库**：当前主版本已升级至 **v10**，npm 最新稳定发行为 **`v10.1.0`**（相较 `v9` 重构了数据加载接口 `setDataLoader`、坐标轴与布局配置）[K1]。
   - **许可证**：`klinecharts/KLineChart` 与 `klinecharts/pro` 均为标准 **Apache-2.0** 许可，仓库无类似 TradingView 的强制品牌徽标外链附加要求 [K1]、[K3]。
   - **原生内置 Overlay 画线与多指标副图能力**：
     - `src/extension/overlay` 原生内置完整几何与交互画线模板（`straightLine`、`rayLine`、`segment`、`horizontalStraightLine`、`verticalStraightLine`、`parallelStraightLine`、`priceLine`、`priceChannelLine`、`fibonacciLine`、`brush`、`simpleAnnotation`、`simpleTag`），通过 `chart.createOverlay(...)` 创建实例，支持人类鼠标锚点交互绘制与程序化传入 `{ timestamp, value }` 坐标创建，并支持 `registerOverlay(...)` 扩展自定义图元（官方 `@klinecharts/extension` / `@klinecharts/pro` 进一步提供江恩箱/角度线、3/5/8 浪与 ABCD/XABCD 形态、多空盈亏比尺、矩形/圆/三角形等模板）[K2]、[K3]。
     - `src/extension/indicator` 原生内置 20+ 种常用主图叠加指标（`MA`、`EMA`、`SMA`、`BOLL`、`SAR`、`BBI`）与独立副图指标（`VOL`、`MACD`、`RSI`、`KDJ`、`WR`、`CCI`、`DMI`、`TRIX`、`OBV`、`PSY` 等），调用 `chart.createIndicator(...)` 即可自动创建独立副图窗格或在指定 `paneId` 堆叠（`isStack: true`），支持 `registerIndicator(...)` 注册自定义指标 [K2]。
   - **核心边界核实**：`klinecharts` 核心包纯零框架依赖（仅渲染 Canvas 画布及内部坐标轴/十字光标/图元交互，不含左侧画线按钮栏、周期切换栏等外围 DOM）；官方开箱即用带工具栏的组件 **`@klinecharts/pro`** 在 `package.json` 中使用 **SolidJS**（`solid-js`）实现弹窗、工具栏与状态响应 [K3]。

#### 架构推断与选型对比

| 评估维度 | `tradingview/lightweight-charts` (`v5.3.0`) | `klinecharts/KLineChart` (`v10.1.0` 核心库) |
|---|---|---|
| **开源许可与界面署名摩擦** | Apache-2.0 + 强制公开页面署名与 `tradingview.com` 外链（默认显示图表内 `attributionLogo`）[L2] | 纯 Apache-2.0，无强制外链徽标要求 [K1] |
| **人类交互画线成本** | **高**：仅提供 Canvas 像素绘制钩子；命中检测、控制点拖拽、磁吸、多点几何需全量自研或依赖成熟度不一的第三方社区包 [L3] | **低**：核心库原生接管多点几何建模、锚点吸附拖拽、悬浮高亮与事件回调，外层只需写一组轻量 React 按钮触发 `createOverlay` [K2] |
| **人机协同双轨共绘契合度** | 需自行设计人机共享图元数据结构并自行实现双向 Canvas 渲染同步 | **天然同构**：人类画线与 Agent 工具调用均统一映射为 `Overlay` 的 `{ name, id, groupId, points: [{ timestamp, value }], styles, extendData }` JSON 结构，双向读写同一实例表 |
| **多指标副图与复盘标记** | v5 支持多 Pane，但所有指标数值计算与柱体/双线配色需外部计算后拆成多个基础 Series 喂入 | 原生内置 20+ 主副图指标计算与多图元（柱/线/圆点）渲染，同时支持 `registerIndicator` 注入自研研究指标 [K2] |
| **外围 UI 集成注意事项** | 无框架耦合（React `useRef` 直接挂载） | 核心库无框架耦合（React `useRef` 直接挂载）；**应规避直接引入基于 SolidJS 的 `@klinecharts/pro` UI 壳**，可仅参考 `@klinecharts/pro` 的扩展图元数学模板（Apache-2.0），用 React 自写工具栏 |

---

### 3.3 `OpenBB` 工作台边界核查：`OpenBB Platform` vs `OpenBB Workspace`（`FINOS Workspace`）

#### 已核实官方事实

1. **2026 年下半年组织与许可证全面转型（切勿沿用 2024–2025 年“前端闭源 + 后端 AGPL”旧印象）**：
   - 官方公告 *"OpenBB belongs to everyone"*（2026 年 8 月发布，2026 年 10 月更新）证实：OpenBB 因原商业化模式未能建立可持续商业闭环，主动终止商业运营并将全部核心代码移交独立非营利开源治理机构 [W1]：
     - **OpenBB Platform / Open Data Platform（ODP）**：仓库迁移至 `openbq-org/OpenBB`，由非营利机构 **OpenBQ Foundation** 托管；最新稳定 Python 发行版 `openbb-v5.0.0`（2026-09-30）采用宽松 **Apache-2.0** 许可（旧版 `v4.7.0` 曾为 `AGPL-3.0`）[W1]、[W2]。
     - **OpenBB Workspace（原 Terminal Pro 前端看板及后端）**：源码已在 `OpenBB-finance/workspace` 公开，并正式移交 Linux 基金会旗下 **FINOS Labs（Fintech Open Source Foundation）** 托管，更名为 **`finos-labs/finos-workspace`（FINOS Workspace）**，根目录 `LICENSE` 明确为 **Apache-2.0**（Copyright © 2026 FINOS, OpenBB, Inc., and BrightQuery，子模块第三方兼容许可记录于 `NOTICE`）；配套 `OpenBB-finance/openbb-copilot` 亦为 **Apache-2.0** [W1]、[W3]、[W4]。
2. **开源后 `OpenBB Workspace / FINOS Workspace` 的真实仓库结构与能力边界**：
   - **全栈单体/多服务形态**：仓库由四部分构成——`frontend/`（在 `OpenBB-finance/workspace` 中目录名为 `terminalpro/`，基于 React 18 + TypeScript + Vite 的单页工作台）、`backend-api/`（基于 FastAPI + SQLAlchemy 的服务端，含用户认证、RBAC 权限、加密数据源凭证管理及管理后台）、`lite/`（Docker Compose 组装脚本，依赖拉取 `ghcr.io/finos-labs/...` 基础镜像）与 `excel-add-in/`（Office.js 插件）[W3]、[W4]。
   - **商业前端组件剥离事实**：核查 `finos-labs/finos-workspace` 官方说明可见，开源核心仓**并未内置或捆绑**早期商业托管版常见的专有付费库（如 `ag-grid-enterprise`、TradingView 商业版 `charting_library` 或 `highcharts`）；核心工作台仅原生渲染基础通用面板（Markdown、HTML、新闻流、PDF、iframe 等），复杂数据表格与专业金融图表需通过外部自定义数据后端（`widgets.json`）或独立 `plugin.json`（`entry.js` / `entry.css`）插件另行接入 [W3]、[W4]。

#### 架构推断与嵌入可行性结论

- **许可证维度（已解除闭源限制）**：截至核查日（2026-11-18），OpenBB Workspace 前端工作台源码在法律许可层面**已属于 Apache-2.0 开源代码**，遵循 Apache-2.0 的版权保留、修改说明与 `NOTICE` 声明义务即可合法修改或商用分发，不再受早期闭源专有条款或 AGPL-3.0 限制。
- **工程嵌入维度（不适合整体嵌入 DeepSeek Harness）**：尽管许可证已放开，**不建议将 OpenBB Workspace / FINOS Workspace 前端代码直接嵌入 DeepSeek Harness 作为看盘与复盘底座**，原因有三：
  1. **宿主层级冲突**：FINOS Workspace 自身就是一个重型顶层门户（绑定独立的 FastAPI + SQLAlchemy 用户/权限/面板布局数据库与整页路由），并非可作为子视图挂载到 DSH 插件槽位的无状态 UI 组件包；
  2. **Agent 协议割裂**：Workspace 围绕自有 `openbb-copilot` / `agents-for-openbb` 与 `widgets.json` REST 拉取范式设计，与 DSH 的本地微内核 `Tool × Store × View` 双向状态流转模型不兼容；
  3. **核心看盘能力仍空白**：开源版核心仓本身并无内置 K 线画线引擎、条件预警或策略回测复盘图，引入后仍须从头集成 `KLineChart` 与回测复盘组件。
- **合理借鉴点**：仅借鉴其 **`widgets.json` 声明式跨面板参数联动元数据设计**（例如切换标的 `symbol` 或快照 `snapshot_id` 时同步驱动 K 线面板、事件日历面板与复盘指标表），以及按需参考 OpenBB Platform v5 的 Python 数据连接器分层模板。

---

### 3.4 策略参数回测寻优与 Web 复盘底座：`freqtrade`（`Hyperopt` + `FreqUI`）vs 直接使用 `Optuna`

#### 已核实官方事实

1. **`Freqtrade` 与 `FreqUI` 的 `GPL-3.0` 许可证边界**：
   - `freqtrade/freqtrade`（当前日历版本至 **`2026.10`**）与官方前端仓库 `freqtrade/frequi` 的 `LICENSE` 均为 **GNU General Public License v3.0（GPL-3.0）** 强著佐权（Strong Copyleft）许可 [F1]、[F4]。
   - 按 GNU GPL-3.0 条款：任何复制、修改、在同进程 `import` `freqtrade` 模块（如 `freqtrade.optimize.hyperopt`）或裁剪复用 `frequi` 前端源码并在后续向第三方分发（convey）的组合软件，整体必须以 GPL-3.0 开源并公开完整源码（与 MIT / Apache-2.0 单向闭源商用或仅宽松开源分发的目标不兼容；注：标准 GPL-3.0 不同于 AGPL-3.0，纯内部运行未分发二进制/源码时不触发网络传染，但一旦将项目打包发布或交付客户即受传染约束）[F1]。
2. **Freqtrade `Hyperopt` 基于 `Optuna` 的寻优机制及过拟合/前视风险**：
   - **寻优实现**：`freqtrade/optimize/hyperopt` 底层封装 **`Optuna`**（独立列于可选依赖 `requirements-hyperopt.txt`）。策略通过声明式参数描述符（`IntParameter`、`DecimalParameter`/`SKDecimal`、`CategoricalParameter`、`BooleanParameter`，`2026.x` 起按策略实例隔离为 `self.<param>.value`）定义 `buy`、`sell`、`roi`、`stoploss`、`trailing`、`protection` 等搜索空间，由 Optuna 采样器（默认支持 `NSGAIIISampler` 等遗传/多目标采样器，亦可通过自定义 `generate_estimator()` 切换 `TPESampler`、`GPSampler`、`CmaEsSampler`、`QMCSampler`）迭代回测并最小化 `IHyperOptLoss` 损失函数（如 `SharpeHyperOptLoss`、`SortinoHyperOptLoss`、`MaxDrawDownHyperOptLoss`）[F1]、[F2]。
   - **前视偏差（Lookahead Bias）与递归偏差放大效应**：官方文档明确警告，一旦策略指标含未来信息泄漏（如反向 `.shift(-1)`、居中滚动窗口、高周期向低周期对齐时引用未收盘未来 K 线）或对历史预热长度敏感的递归公式，Optuna 会在数百至数千轮迭代中主动锁定并放大该漏洞，产出样本内极高胜率、实盘完全失效的虚假最优解。为此 Freqtrade 专门内置两个独立 CLI 审计工具：**`freqtrade lookahead-analysis`**（通过截断不同时点尾部 K 线重跑信号，核验历史买卖点是否随未来数据改变）与 **`freqtrade recursive-analysis`**（对比不同 `startup_candle_count` 窗口长度下的指标数值漂移）[F2]、[F3]。
   - **过拟合（Overfitting）边界**：官方文档指出同时放开 `--spaces all` 或给定过宽浮点区间极易造成严重样本内曲线拟合；且 `freqtrade hyperopt` **不会自动强制执行 Walk-Forward 滚动样本外验证**，依赖使用者手工用 `--timerange` 切分寻优期与独立盲测期 [F2]。
   - **`FreqUI` 复盘面板边界**：基于 Vue 3 构建，内部使用 **Apache ECharts**（而非 Lightweight Charts）渲染 `CandleChart` K 线、指标与回测成交标记，强耦合 Freqtrade 自身的加密资产交易对 RPC/REST 接口与机器人状态机 [F4]。
3. **`Optuna`（及 `optuna-dashboard`）自身许可证与能力边界**：
   - **版本与许可证**：`optuna/optuna` 最新稳定版为 **`v5.1.0`**（引入 v5 架构与 Rustuna 生态支持），许可证为 **MIT**；官方可视化面板 `optuna/optuna-dashboard` 亦为 **MIT**，可无传染风险地集成进自有开源或商业代码库 [P1]、[P3]。
   - **已核实核心能力**：Define-by-run 动态条件搜索空间（`trial.suggest_int / suggest_float / suggest_categorical`）；单目标与多目标优化（`TPESampler`/MOTPE 适合中小预算与条件空间，`NSGAIIISampler` 适合 3+ 目标帕累托前沿）；软约束支持（`trial.set_constraint`）；持久化存储支持关系型 `RDBStorage`（SQLite/PostgreSQL）与基于追加日志、适合多进程并发的 **`JournalStorage`**（`JournalFileBackend` JSONL）；`optuna-dashboard` 原生提供帕累托前沿图、超参数重要性（fANOVA/MDI）、等高线图与平行坐标图 [P2]、[P3]。
   - **已核实能力边界**：
     1. **多目标剪枝限制**：Optuna 官方文档载明，内置中间汇报 `trial.report()` 与标准剪枝器（`MedianPruner`、`HyperbandPruner`）仅面向单目标 Study，在多目标 Study 中直接调用 `trial.report()` 会原生抛出 `NotImplementedError` [P2]；
     2. **零金融防线**：Optuna 对时间序列因果性、金融交易摩擦与统计多重假设检验一无所知——**不提供**回测引擎、**不提供**防信息泄漏的时序切分器（Walk-Forward / Purged Embargo K-Fold）、**不提供**前视偏差检测，也**不提供**试验次数膨胀后的统计显著性折扣（如 Deflated Sharpe Ratio 或回测过拟合概率 PBO）。

#### 架构推断与选型结论

- **底座取舍**：直接选用 **`Optuna`（MIT）** 作为 Talaria Python 研究后端的参数寻优引擎；**完全排除引入 `Freqtrade` 或 `FreqUI`（GPL-3.0）源码**。Web 复盘面板无需依赖 `FreqUI`，因为 Talaria 主界面已选用 `KLineChart`（可直接在主图叠加回测买卖标记与净值/回撤副图），寻优分析图表则可在开发期直接挂载 `optuna-dashboard`（MIT）或将其 JSON/JournalStorage 试验记录投影到工作台复盘面板中（读取等高线、帕累托前沿与参数邻域平坦度）。
- **必须洁净室自建的三道防过拟合/防前视护栏**：不能把 `Optuna` 裸露给用户或大模型无约束刷分，必须在 Talaria 回测寻优层强制前置：
  1. **截断式前视与预热一致性门禁**：在启动任何 `study.optimize()` 前，自动抽取若干历史截断点校验 `t` 时刻及之前的信号/特征在 `T_short` 与 `T_full` 截断快照下完全逐位一致（借鉴 `lookahead-analysis` 与 `recursive-analysis` 原理洁净室自研）；
  2. **强制样本内/验证集/样本外盲测三段隔离与试验计数留痕**：Study 绑定不可变数据快照 `snapshot_id`，严格禁止优化器读取样本外（Holdout OOS）区间，并在实验记录（`JournalStorage` + SQLite manifest）中完整记录总 `n_trials`（包括被丢弃的失败试验），防止“挑樱桃”幸存者汇报；
  3. **邻域稳健性与多目标风险约束**：优先采用带约束的双目标（如风险调整收益 vs 最大回撤，`trial.set_constraint` 限制最低有效交易笔数）并评估最优参数周边的平坦度（高原区优于孤立尖峰）。

---

## 4. Talaria 人机协同工作台组合建议与最小防线契约

以下组合为基于上述一手事实推导的 **Talaria 架构建议**（非任一外部仓库原样成品）：

| 工作台子域 | 推荐开源组件 / 许可证 | 复用或借鉴的外部经验 | 必须由 Talaria 自研补齐的最小契约 |
|---|---|---|---|
| **主图看盘与人机双轨画线** | `klinecharts` `v10.1.0`（Apache-2.0）+ 自研薄 React 工具栏 | 借鉴 `maddogfinance/dsh-trading`（MIT）的 `annotate_chart` 真实 K 线价格范围硬门控、溯源字段与 `get_chart_view` 视口同步 | 统一定义 `OverlayRecord` JSON Schema（区分 `author: human \| agent`、关联 `evidence_refs` 与价格越界校验），持久化到本地 Store 供人机双向增删改与撤销 |
| **自选列表与条件预警** | 自研 Store + UI（不引入 PolyForm 许可代码） | 洁净室参考 `Tool × Store × View` 单一事实源与跨面板 `symbol` 参数联动思想 | 建立显式规则引擎（价格阈值、指标交叉、画线触碰/突破），记录规则版本、触发时刻、冷却去重窗口与触发时的行情快照引用 |
| **事件日历与资讯对照** | OpenBB Platform v5 provider 模板（Apache-2.0，按需接入合规数据源） | 区分“盘中实时日历查询”与“历史 PIT 事件仓库”（结合 `trading-data-templates-sources.md` 契约） | 事件表分离 `event_period`、`scheduled_at`、`published_at`、`available_at`；支持映射到 `KLineChart` 时间轴标注，且回测仅可见 cutoff 前已公开记录 |
| **模拟/实盘持仓台账与复盘** | 自研不可变账本（Parquet/DuckDB + SQLite）+ 前端复盘视图 | 洁净室参考外部导入先入 `staged` 待人工确认后再入正式账本的安全流转模式 | 区分现金流水账、持仓批次账（支持 FIFO 及除权分红事件调整）与策略信号归因，回测成交流水与 paper 台账共享同一套复盘指标与主图打点协议 |
| **策略参数回测寻优** | `optuna` `v5.1.0`（MIT）+ `JournalStorage`（可选开发期配合 `optuna-dashboard` MIT） | 洁净室吸收 Freqtrade“先跑前视截断与预热漂移审计，再做受限空间样本外寻优”的纪律 | 寻优前置截断一致性测试；强制绑定 `snapshot_id` 与 Train / Val / Holdout OOS 切分；完整持久化全部 `n_trials` 并检验参数邻域稳健性，禁止对样本外集二次调参 |

---

## 5. 一手来源索引

以下链接均为官方 GitHub 仓库、官方文档、官方公告与包发行元数据，核查基准日为 **2026-11-18**：

- **[D1] `zhu1090093659/dsh-trading` 许可证、安全门禁与总览：** [官方仓库主页](https://github.com/zhu1090093659/dsh-trading)、[LICENSE（PolyForm Noncommercial 1.0.0）](https://github.com/zhu1090093659/dsh-trading/blob/main/LICENSE)、[README.md](https://github.com/zhu1090093659/dsh-trading/blob/main/README.md)。
- **[D2] `zhu1090093659/dsh-trading` 包结构与三元架构契约：** [packages 目录](https://github.com/zhu1090093659/dsh-trading/tree/main/packages)、[docs/design/agentic-native-architecture.md](https://github.com/zhu1090093659/dsh-trading/blob/main/docs/design/agentic-native-architecture.md)、[docs/design/knowledge-graph.md（含 `TvChart.tsx` 封装说明）](https://github.com/zhu1090093659/dsh-trading/blob/main/docs/design/knowledge-graph.md)。
- **[D3] `zhu1090093659/dsh-trading` 策略板块与单标的回测契约：** [docs/design/strategy-tab.md](https://github.com/zhu1090093659/dsh-trading/blob/main/docs/design/strategy-tab.md)、[packages/strategies](https://github.com/zhu1090093659/dsh-trading/tree/main/packages/strategies)。
- **[D4] `zhu1090093659/dsh-trading` 统一资产台账契约：** [docs/design/holdings-ledger.md](https://github.com/zhu1090093659/dsh-trading/blob/main/docs/design/holdings-ledger.md)。
- **[D5] `zhu1090093659/dsh-trading` 金十 MCP 快讯与财经日历连接器：** [packages/connector-jin10](https://github.com/zhu1090093659/dsh-trading/tree/main/packages/connector-jin10)。
- **[D6] 同名生态辨析 `maddogfinance/dsh-trading`（MIT 许可与 `annotate_chart` 门控实现）：** [官方仓库主页](https://github.com/maddogfinance/dsh-trading)、[LICENSE（MIT）](https://github.com/maddogfinance/dsh-trading/blob/main/LICENSE)、[README.md（含 `annotate_chart` 溯源与价格范围门控说明）](https://github.com/maddogfinance/dsh-trading/blob/main/README.md)。
- **[L1] TradingView Lightweight Charts 版本与发布元数据：** [官方仓库主页](https://github.com/tradingview/lightweight-charts)、[GitHub Releases](https://github.com/tradingview/lightweight-charts/releases)、[npm `lightweight-charts` 包主页（`v5.3.0`）](https://www.npmjs.com/package/lightweight-charts)。
- **[L2] Lightweight Charts 许可证与归属徽标条款：** [LICENSE（Apache-2.0）](https://github.com/tradingview/lightweight-charts/blob/master/LICENSE)、[NOTICE（TradingView 归属声明与外链要求）](https://github.com/tradingview/lightweight-charts/blob/master/NOTICE)、[README Attribution 章节与 `attributionLogo` 说明](https://github.com/tradingview/lightweight-charts/blob/master/README.md)。
- **[L3] Lightweight Charts Primitives 扩展机制与示例边界：** [官方文档 Series Primitives](https://tradingview.github.io/lightweight-charts/docs/plugins/series-primitives)、[官方文档 Pane Primitives](https://tradingview.github.io/lightweight-charts/docs/plugins/pane-primitives)、[plugin-examples 源码目录](https://github.com/tradingview/lightweight-charts/tree/master/plugin-examples)。
- **[K1] KLineChart 版本与许可证：** [官方仓库主页](https://github.com/klinecharts/KLineChart)、[LICENSE（Apache-2.0）](https://github.com/klinecharts/KLineChart/blob/main/LICENSE)、[GitHub Releases](https://github.com/klinecharts/KLineChart/releases)、[npm `klinecharts` 包主页（`v10.1.0`）](https://www.npmjs.com/package/klinecharts)。
- **[K2] KLineChart 原生 Overlay 画线与多指标副图实现：** [官方文档 Overlay 画线指南](https://klinecharts.com/guide/overlay)、[官方文档 Indicator 技术指标指南](https://klinecharts.com/guide/indicator)、[`src/extension/overlay` 内置画线源码](https://github.com/klinecharts/KLineChart/tree/main/src/extension/overlay)、[`src/extension/indicator` 内置指标源码](https://github.com/klinecharts/KLineChart/tree/main/src/extension/indicator)。
- **[K3] KLineChart Pro 工具栏封装与 SolidJS 依赖边界：** [官方仓库 `klinecharts/pro`](https://github.com/klinecharts/pro)、[LICENSE（Apache-2.0）](https://github.com/klinecharts/pro/blob/main/LICENSE)、[`package.json`（`solid-js` UI 框架依赖）](https://github.com/klinecharts/pro/blob/main/package.json)。
- **[W1] OpenBB 2026 转型与开源治理移交公告：** [OpenBB 官方博客《OpenBB belongs to everyone》](https://openbb.co/blog/openbb-belongs-to-everyone)、[OpenBQ Foundation 组织仓库](https://github.com/openbq-org)、[FINOS 官方项目介绍](https://www.finos.org/)。
- **[W2] OpenBB Platform v5 版本与许可证：** [`openbq-org/OpenBB` 仓库](https://github.com/openbq-org/OpenBB)、[`openbb-v5.0.0` Release](https://github.com/openbq-org/OpenBB/releases/tag/openbb-v5.0.0)、[LICENSE（Apache-2.0）](https://github.com/openbq-org/OpenBB/blob/develop/LICENSE)。
- **[W3] OpenBB Workspace 原始开源仓与 Copilot：** [`OpenBB-finance/workspace` 仓库](https://github.com/OpenBB-finance/workspace)、[`OpenBB-finance/workspace` LICENSE（Apache-2.0）](https://github.com/OpenBB-finance/workspace/blob/main/LICENSE)、[`OpenBB-finance/openbb-copilot` 仓库（Apache-2.0）](https://github.com/OpenBB-finance/openbb-copilot)。
- **[W4] FINOS Workspace 全栈结构与组件边界：** [`finos-labs/finos-workspace` 仓库](https://github.com/finos-labs/finos-workspace)、[LICENSE（Apache-2.0）与 NOTICE](https://github.com/finos-labs/finos-workspace/blob/main/LICENSE)、[README.md（架构目录、`widgets.json` / `plugin.json` 与无内置商业图表库边界）](https://github.com/finos-labs/finos-workspace/blob/main/README.md)。
- **[F1] Freqtrade 版本与 GPL-3.0 许可证：** [`freqtrade/freqtrade` 仓库](https://github.com/freqtrade/freqtrade)、[LICENSE（GPL-3.0）](https://github.com/freqtrade/freqtrade/blob/develop/LICENSE)、[GitHub Releases（`2026.10` 系列）](https://github.com/freqtrade/freqtrade/releases)。
- **[F2] Freqtrade Hyperopt 基于 Optuna 的实现与过拟合警示：** [官方文档 Hyperopt](https://www.freqtrade.io/en/stable/hyperopt/)、[`freqtrade/optimize/hyperopt` 源码目录](https://github.com/freqtrade/freqtrade/tree/develop/freqtrade/optimize/hyperopt)。
- **[F3] Freqtrade 前视偏差与递归指标诊断工具：** [官方文档 Lookahead Analysis](https://www.freqtrade.io/en/stable/lookahead-analysis/)、[官方文档 Recursive Analysis](https://www.freqtrade.io/en/stable/recursive-analysis/)。
- **[F4] FreqUI 许可证与前端图表实现边界：** [`freqtrade/frequi` 仓库](https://github.com/freqtrade/frequi)、[LICENSE（GPL-3.0）](https://github.com/freqtrade/frequi/blob/develop/LICENSE)。
- **[P1] Optuna 版本与 MIT 许可证：** [`optuna/optuna` 仓库](https://github.com/optuna/optuna)、[LICENSE（MIT）](https://github.com/optuna/optuna/blob/master/LICENSE)、[GitHub Releases（`v5.1.0`）](https://github.com/optuna/optuna/releases)。
- **[P2] Optuna 采样器、多目标剪枝边界与 JournalStorage 文档：** [Optuna 官方文档主页](https://optuna.org/)、[Samplers（`TPESampler` / `NSGAIIISampler`）文档](https://optuna.readthedocs.io/en/stable/reference/samplers/index.html)、[Multi-objective Optimization 与剪枝限制文档](https://optuna.readthedocs.io/en/stable/tutorial/20_recipes/002_multi_objective.html)、[`JournalStorage` 文档](https://optuna.readthedocs.io/en/stable/reference/generated/optuna.storages.JournalStorage.html)。
- **[P3] Optuna Dashboard 许可证与可视化能力：** [`optuna/optuna-dashboard` 仓库](https://github.com/optuna/optuna-dashboard)、[LICENSE（MIT）](https://github.com/optuna/optuna-dashboard/blob/main/LICENSE)、[GitHub Releases](https://github.com/optuna/optuna-dashboard/releases)。
