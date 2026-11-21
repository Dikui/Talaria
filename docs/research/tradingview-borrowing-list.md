# 向 TradingView 借鉴：图表、指标与交互清单

- **日期**：2026-11-21
- **目的**：用户已经习惯 TradingView，希望 Talaria 的图表和指标用起来和它一致。本文列出值得对齐的功能、Talaria 的现状、可以参照的开源项目或公开文档，以及内置指标的对照。
- **口径**：只借鉴产品设计和概念模型，由 Talaria 自己实现。不连接 TradingView 的接口，不使用它的数据或指标输出，也不拿它的输出当测试基准。指标按 TradingView 公开的定义在本地 Python 计算，符合 ADR-0010。
- **与已有调研的关系**：许可证与条款风险见 [tradingview-reverse-engineering.md](./tradingview-reverse-engineering.md)；图表库选型见 [copilot-trading-workbench-sources.md](./copilot-trading-workbench-sources.md) §3.2；图表规则脚本见 [chart-rule-scripting-sources.md](./chart-rule-scripting-sources.md)。
- **标注约定**：**事实**＝来源可核对；**现状**＝读 Talaria 代码得出；**建议**＝面向 Talaria 的判断。
- **核实情况（2026-11-21 第二轮）**：标 [*] 的来源已逐字核对原文或源码文件（Advanced Charts 文档原句、GitHub API 返回的许可证与文件树、LICENSE 原文）；未标的是帮助中心页面的转述，或者仍待核实（§7）。
- **已定决策（2026-11-21，用户确认）**：画线和指标先只做最常用的，其余以后补；默认参数可以随意调整（尚未投入使用），但最终要允许用户自己设默认值（§5.1）。

---

## 1. 结论

1. **差距最大的是画线和指标数量。** TradingView 的 Advanced Charts 有 84 种画线工具和 100 多个指标 [AC-DRAW*][AC-IND*]；Talaria 目前有 7 种画线、8 个指标（现状）。按已定决策只补最常用的十几种（§2.2、§5）。
2. **两个基于 KLineChart 的开源项目已经实现了大部分 TradingView 式交互，可以直接参考。** `aztharion/klinechart-ui-core`（Apache-2.0）有 26 个画线模块（含多头/空头仓位、测量、斐波那契扩展）、多图表同步、命令面板和撤销重做 [KUC*]；`NemeZZiZZ/react-klinecharts-ui`（LICENSE 为 Apache-2.0）有 26 个画线模块、覆盖画线和指标的撤销重做、多图同步 [RKU*]。两者都是小项目（star 数 25 和 19），只作参考，不作依赖（建议）。两者都没有对象树，这部分要自己做。
3. **从逆向项目的源码能看到 TradingView 的几个概念模型，值得照搬到 Talaria 的契约里：** 行情会话和图表会话分开；指标实例由“指标 id + 带类型的输入列表”描述；指标除了输出线，还能输出标签、线段、方框、表格等图形 [MC-STUDY][MC-GFX]。后两点正好是分析 02、预警与扫描 01–02（组件模型、JSON 规则、规则编辑器）需要定的接口（§4）。
4. **已有指标里 RSI、布林带、SMA 与 TradingView 一致；EMA、ATR、VWAP 有三处不一致。** 对照 Pine 参考手册原文 [PINE-REF*]：EMA 以第一个值作种子（Talaria 用前 N 根 SMA）；ATR 第一根的真实波幅取 高 − 低（Talaria 跳过第一根）；VWAP 默认按日锚定、首个日界之前不出值（Talaria 默认从第一根一直累计）。前两处只影响开头约 3N 根，之后收敛；VWAP 影响全程。按“以 TradingView 为准”，三处都改，黄金向量随之重生成。EMA、SMA 的默认周期改为 9（§5）。
5. **建议分三批落地：** 第一批进图表类（新开任务单），补交互骨架（对象树、撤销重做、快捷键、磁吸、锁定和隐藏）；第二批补高频画线和 MACD、Stochastic 等常用指标；第三批随分析 02 和预警与扫描 02 定指标输入 Schema 和图形输出（§6）。

---

## 2. 功能对照

### 2.1 总表

| 功能 | TradingView（事实） | Talaria 现状 | 参照 | 建议优先级 |
|---|---|---|---|---|
| 多图表布局与同步 | 多图表布局是 Trading Platform 功能，可按品种、周期、十字线、时间、日期范围同步 [TV-MCL] | 单图；平台 02 做多页面，原型已验证跨窗口同步标的、周期和十字线 | `klinechart-ui-core` 的 `WorkspaceProvider`、`useChartSync`（2x1、2x2、1x3 网格，同步十字线、滚动、缩放、品种、周期）[KUC]；`react-klinecharts-ui` 同名 hook [RKU] | 高：平台 02 |
| 画线工具 | 6 类共 84 种（§2.2）[AC-DRAW] | 7 种：水平线、射线、线段、平行通道、斐波那契回撤、矩形、文本 | `klinechart-ui-core` 26 个画线模块 [KUC*]；`lightweight-charts-drawing` 86 种，MIT [LCD*] | 高 |
| 画线辅助操作 | 测量、放大、弱磁吸和强磁吸、保持画线模式、锁定全部、隐藏全部、删除全部 [AC-DRAW*]（文档只列名称，没有说明行为） | 有画线管理器和检查器；无磁吸、锁定、隐藏全部 | `lightweight-charts-drawing` 有 `locked`、`hidden`、置顶和置底 [LCD] | 高 |
| 对象树 | 管理（隐藏、显示、删除）图上的画线、指标和品种 [AC-OBJ*] | 无统一列表；画线管理器只管画线 | 两个 KLineChart 项目的文件树里都没有对象树 [KUC*][RKU*]，需要自己做；`lightweight-charts-drawing` 的 `locked`、`hidden`、置顶和置底可参考 [LCD] | 高 |
| 撤销与重做 | 产品内置（未核实具体范围） | 画线已有撤销重做，基于后端版本链（`drawings/history.ts`）；指标设置等其他操作不能撤销 | `react-klinecharts-ui` 的 `useUndoRedo` 自动接入画线和指标，快捷键 `Ctrl+Z` 撤销、`Ctrl+Y` / `Ctrl+Shift+Z` 重做 [RKU*]；`klinechart-ui-core` 也有 `useUndoRedo` [KUC*] | 中 |
| 指标数量 | “more than 100 indicators”；库里不支持 Pine [AC-IND*] | 8 个（§5） | 指标公式见帮助中心各篇（§5） | 中：按需补 |
| 指标设置 | 每个指标有输入和样式两组设置；API 用 `createStudy(名称, …, {in_0: …})`，可查询输入列表 [AC-IND] | 指标工具栏只能改周期、标准差倍数、VWAP 锚点 | `react-klinecharts-ui` 的 `getIndicatorParams` 返回 `[{label, defaultValue}]` [RKU] | 高：随分析 02 定输入 Schema |
| 指标模板与布局保存 | 保存的布局包含画线、指标和图表设置（颜色、样式），不含可见时间范围；指标模板是一组已加载的指标及其输入和样式；除精度外的设置都保存 [AC-SAVE*] | 画线、自选、预警存在服务端；指标设置和布局不持久化 | `react-klinecharts-ui` 的 `useLayoutManager`（保存、加载、重命名、删除、自动保存；来自 README 摘要，未逐字核对）[RKU]；`klinechart-ui-core` 的布局预设 [KUC] | 中 |
| 快捷键 | 未核实（帮助中心原页面已 404） | 只有 Escape、Enter 等零散按键 | `klinechart-ui-core` 用 `Cmd+K` 命令面板搜品种、指标和布局 [KUC]；`lightweight-charts-drawing`：Escape 逐级取消，Delete 删除选中，Shift 按 45° 约束 [LCD] | 中 |
| 从图表建预警 | 产品内置 | 已有：画线和关键位可以直接挂预警；预警与扫描 01–02 做组合规则 | — | 已在任务单 |
| K 线回放 | 未核实 | 后端有回放传输层；回测与研究 01 是批量回测，不是看盘回放 | — | 低：先不做 |
| 周期写法 | 纯数字表示分钟（`1`、`60`、`240`），`S`、`D`、`W`、`M` 为秒、日、周、月 [MC-TF] | `1m`、`5m`、`1h`、`4h`、`1d` | — | 低：内部契约不改；输入框可以同时接受两种写法 |

### 2.2 画线工具：TradingView 的分类与建议补齐范围

TradingView 的分类（事实，[AC-DRAW*]；趋势类和预测测量类的工具名已逐字核对，数量是按页面列表自行计数）：

| 类别 | 数量 | 代表工具 |
|---|---|---|
| 趋势 | 15 | 趋势线、箭头、射线、信息线、延长线、趋势角度、水平线、水平射线、垂直线、十字线、平行通道、回归趋势、平顶/平底、不相交通道、锚定 VWAP |
| 江恩与斐波那契 | 19 | 斐波那契回撤、基于趋势的斐波那契扩展、叉形线及其变体、斐波那契通道、时间周期、江恩箱与江恩扇形、速度阻力扇形等 |
| 几何形状 | 12 | 画笔、高亮笔、矩形、圆、椭圆、路径、曲线、折线、三角形、旋转矩形、弧线 |
| 标注 | 14 | 文本、便签、锚定便签、路标、标注框、评论、价格标签、价格便签、箭头标记、旗帜 |
| 形态 | 14 | XABCD、ABCD、三角形、三驱动、头肩、艾略特波浪（三种）、周期线、正弦线 |
| 预测与测量工具 | 10 | 多头仓位、空头仓位、仓位预测、日期范围、价格范围、日期价格范围、K 线形态、幽灵行情、扇区、固定范围成交量分布 |

按已定决策，第二批只补最常用的工具，其余以后再补。排序依据是通用的交易员使用习惯和 Talaria 的交易计划流程，没有使用数据（建议）：

1. **多头仓位和空头仓位**：直接对应交易计划的入场、止损、止盈和盈亏比，可以作为图表人工下单（交易 04）的界面入口。
2. **价格范围、日期范围、日期价格范围**：测量工具，使用频率高。
3. **趋势线（可两端延长）、水平射线、垂直线、十字线**：补全趋势类的基础工具。
4. **基于趋势的斐波那契扩展**：和已有的回撤配套。
5. **价格标签、箭头标记**：轻量标注，智能体会诊的标注也能复用。
6. **锚定 VWAP**：和关键位体系相关，但计算要在后端（ADR-0010）。
7. **固定范围成交量分布**：`klinechart-ui-core` 已实现 VPVR/FRVP，可以参考 [KUC]。计算也要在后端。

叉形线、江恩、形态、几何画笔类的工具先不做。

1–4 在 `klinechart-ui-core` 的画线模块里都有对应文件（`longPosition.ts`、`shortPosition.ts`、`measure.ts`、`ray.ts`、`fibonacciExtension.ts` 等）[KUC*]，可以先读它的实现再写 Talaria 自己的版本。

---

## 3. 开源参照项目

| 项目 | 许可证 | star / 最近推送 | 技术栈 | 可借鉴的部分 |
|---|---|---|---|---|
| `aztharion/klinechart-ui-core` [KUC*] | Apache-2.0（GitHub API 与 LICENSE 一致；LICENSE 无版权人行，第 1 节措辞与标准文本略有不同） | 25 / 2026-11-11 | React 19、Tailwind v4、Radix UI，基于 KLineChart | `src/core/headless/overlays/` 下 26 个画线模块（多头/空头仓位、测量、射线、平行通道、斐波那契回撤与扩展、江恩箱与扇形、艾略特波浪、XABCD 等）；止损止盈订单线和预警线；多图同步（`useChartSync`、`WorkspaceProvider`）；命令面板（`CommandPalette.tsx`）；撤销重做（`useUndoRedo.ts`）。以上按文件名确认，未读代码内容 |
| `NemeZZiZZ/react-klinecharts-ui` [RKU*] | LICENSE 文件是 Apache-2.0 全文（版权 2025 NemeZZiZZ），但 GitHub API 识别为 NOASSERTION；部分代码移植自 `dsavenk0/KLineChart-Pro`，该项目为 Apache-2.0 | 19 / 2026-10-17 | Headless React hook，基于 KLineChart，不带界面组件 | `src/overlays/` 下 26 个画线模块；`useUndoRedo` 自动接入画线和指标；`useHotkeys`（README 只举了 `Ctrl/Cmd+L` 清空画线的例子）；多图同步 `useChartSync`；README 自述“9 drawing overlays”与实际文件数不符 |
| `difurious/lightweight-charts-line-tools-core` [LTC*] | MPL-2.0 | 107 / 2026-07-16 | Lightweight Charts v5 插件，非 React | 21 种画线的注册机制（`registerLineTool`）；双击事件打开属性面板；`exportLineTools` / `importLineTools` 按 id 合并导入 |
| `deepentropy/lightweight-charts-drawing` [LCD*] | MIT | 157 / 2026-11-19 | 与框架无关的 TypeScript，基于 Lightweight Charts v5 | `docs/TOOLS.md` 列出 86 种工具，分 17 个小类，大类与 TradingView 一致；键盘交互规则；`locked`、`hidden`、层级；撤销步骤合并事件；JSON 导入导出 |

**建议**：交互和组件结构优先参考前两个（与 Talaria 同为 KLineChart）。后两个基于 Lightweight Charts，几何代码不能直接搬，但工具分类、数据结构和键盘规则可以参考。MPL-2.0 是文件级 copyleft，如果复制其中的文件，修改过的文件要保持 MPL 开源；只参考设计则没有这个问题。

---

## 4. 从逆向项目源码看到的概念模型

`Mathieu2301/TradingView-API` 逆向了 TradingView 的私有协议。我们不运行它，只读源码，了解 TradingView 内部怎么组织这些概念。

| 概念 | 源码中的事实 | 对 Talaria 的借鉴（建议） |
|---|---|---|
| 行情会话与图表会话分开 | `QuoteSession` 负责报价，可按品种增删订阅和字段；`ChartSession` 负责某个品种、某个周期的 K 线序列，指标实例挂在图表会话上 [MC-QS][MC-CS] | 数据存取 06 的订阅接口可以分两类：报价（多品种、只要最新值）和序列（单品种单周期、要历史加增量）。现有 `/ws/desk` 把两者混在一起 |
| 指标实例 | 指标 id 形如 `STD;RSI`（内置）、`PUB;…`（公开）、`USER;…`（用户）；输入按 `in_0`、`in_1` 编号，每个输入有名称、类型、取值和可选项；类型有 text、source、integer、float、resolution、bool、color、session、symbol、time、price、textarea；`setInput` 会校验类型和可选项 [MC-STUDY] | 分析 02 的组件输入 Schema 可以照这个思路：组件 id 带来源前缀（内置、用户、智能体），输入用带类型和约束的列表描述，前端按 Schema 自动生成设置表单。预警与扫描 02 的规则编辑器也按 Schema 生成表单 |
| 内置指标默认值 | `BuiltInIndicator` 为每种类型声明默认参数，未知参数默认拒绝 [MC-BUILTIN] | 和 Talaria 现有 `operators/spec.py` 的做法一致，保留 |
| 指标输出图形 | 除了输出线，指标还能输出标签、线段、方框、表格、水平线、多边形、横向直方图，并以增量命令（创建、擦除）更新 [MC-GFX] | 分析 02 与预警与扫描 02 的组件输出不应只有数值序列。关键位、信号标记、成交量分布都可以统一成“图形输出”，前端只负责渲染（符合 ADR-0010） |
| 周期写法 | 纯数字表示分钟，后缀 `S`、`D`、`W`、`M` [MC-TF] | 内部继续用 `1m`、`1h`；如果要做“直接键入数字切周期”，在输入层做转换即可 |

---

## 5. 内置指标对照

TradingView 列为事实（[TV-*]）；Talaria 列为现状（`core/src/talaria/foundation/operators/core.py`、`dsh/.../indicators/config.ts`）。

| 指标 | TradingView 默认与算法 | Talaria 现状 | 差异与处理（建议） |
|---|---|---|---|
| SMA | 长度 9，源为收盘价；最近 N 个值之和除以 N [TV-SMA] | 长度 20；算法相同；不足 N 根时为空 | 默认周期不同，见 §5.1 |
| EMA | 长度 9，源为收盘价 [TV-EMA]；alpha = 2/(长度+1)；参考代码 `pine_ema` 为 `sum := na(sum[1]) ? src : alpha * src + (1 - alpha) * nz(sum[1])`，**以第一个源值作种子**，从第一根起就有值 [PINE-REF*] | 长度 20；alpha 相同；**前 N−1 根为空，第 N 根以前 N 根的 SMA 作种子** | **不一致**：前约 3N 根数值不同，之后收敛。按“以 TradingView 为准”改为第一个源值作种子，黄金向量随之重生成 |
| RSI | 长度 14，源为收盘价；复现代码用 `rma(gain, 14)` 和 `rma(loss, 14)`，与内置 `rsi(close, 14)` 完全相等 [TV-RSI]；`pine_rma` 为 `sum := na(sum[1]) ? ta.sma(src, length) : alpha * src + (1 - alpha) * nz(sum[1])`，alpha = 1/长度，**以前 N 个值的 SMA 作种子** [PINE-REF*] | 长度 14；RMA，前 N 个变化量的均值作种子 | 一致 |
| ATR | 长度 14；默认用 RMA 平滑，可改为 SMA、EMA、WMA [TV-ATR]；参考代码 `pine_atr` 在第一根（没有前一根）时真实波幅取 **高 − 低**，再做 `ta.rma` [PINE-REF*]。另：`ta.tr` 变量等同 `ta.tr(handle_na = false)`，第一根返回 na [PINE-REF*] | 长度 14；RMA；**第一根不计真实波幅**，RMA 种子用第 2 至第 N+1 根 | **不一致**：TradingView 的种子窗口包含第一根的 高 − 低，Talaria 往后错了一根，前约 3N 根数值不同。按 `pine_atr` 对齐；平滑方式可选，列入指标设置 |
| 布林带 | 长度 20，源为收盘价，倍数 2；中轨为 SMA，上下轨为中轨 ± 倍数 × 标准差；“中轨均线类型”可选 [TV-BB]；`ta.stdev` 的 `biased` 默认 `true`，即总体标准差（除以 N）[PINE-REF*] | 长度 20，倍数 2；中轨 SMA；总体标准差 | 一致；中轨均线类型可列入设置 |
| VWAP | 典型价 hlc3 × 成交量的累计 ÷ 成交量累计；锚定周期可选会话、周、月、季、年等；有标准差带和百分比带 [TV-VWAP]；`ta.vwap` 的 `anchor` 默认等同 `timeframe.change("1D")`，即**按日重置**；**第一次触发锚点之前返回 na** [PINE-REF*] | 典型价 hlc3；默认锚点为“快照起点”（从第一根一直累计），可选“按 UTC 日” | **不一致**：默认改为按日锚定，且在第一个日界之前不出值；增加周、月锚点和标准差带。“日”的边界在 Pine 里取交易所会话，7×24 加密市场 Talaria 继续按 UTC 0 点 |
| MACD | 快线均线 − 慢线均线；信号线为 MACD 的均线；柱状图 = MACD − 信号线；振荡器和信号线的均线类型可选 EMA 或 SMA [TV-MACD]；`ta.macd` 示例用 `ta.macd(close, 12, 26, 9)` [PINE-REF*] | 无 | 第二批补，默认 12、26、9（来自示例，内置指标的默认值未单独核实） |
| Stochastic | %K = SMA(100 × (收 − 最低) / (最高 − 最低), 平滑 K)；%D = SMA(%K, D 周期)；默认 K 长度 14、平滑 3、D 3，阈值 20/80 [TV-STOCH] | 无 | 第二批补 |
| 资金费率分位、OI 背离、净赔率 | TradingView 无对应内置指标 | 有（Talaria 自有） | 保留，属于 Talaria 的特色 |

### 5.1 默认参数

EMA 和 SMA 的默认周期，TradingView 是 9，Talaria 是 20（事实与现状）。

**已定（2026-11-21）**：Talaria 尚未投入使用，默认参数可以直接改，EMA、SMA 改为 9，其他指标的默认值也以 TradingView 为准。同时要让用户能自己设默认值：

- 现在的默认值写死在两处：后端 `operators/spec.py` 和前端 `indicators/config.ts`（现状）。用户在指标工具栏改的参数不保存，刷新后回到默认值。
- 建议参照 TradingView 的指标模板 [AC-SAVE*]：用户可以把当前参数存为自己的默认值或命名模板，存在服务端，图表、预警和规则编辑器新建指标时都读这份默认值。代码里的默认值只作出厂值。
- 已保存的预警和规则必须显式记录参数，不跟随默认值变化。

---

## 6. 建议的落地顺序

| 批次 | 内容 | 归属 |
|---|---|---|
| 第一批：交互骨架 | 对象树（画线、指标、关键位统一列表，可隐藏、锁定、删除）；把画线的撤销重做扩展到指标增删和设置；快捷键与命令面板；画线磁吸、保持画线模式、锁定和隐藏全部 | 图表类新开任务单 |
| 第二批：工具与指标 | §2.2 列出的 7 组画线；MACD、Stochastic；ATR 平滑方式、布林带中轨类型、VWAP 周和月锚点与标准差带；EMA/SMA 默认周期改为 9；EMA 种子、ATR 第一根、VWAP 默认锚点按 Pine 参考手册对齐（§5）；用户自定义默认值与指标模板（§5.1） | 新开任务单；指标部分按 ADR-0010 在 Python 实现并补黄金向量 |
| 第三批：模型 | 组件输入 Schema（带类型的输入列表，前端据此生成设置表单）；组件图形输出（标签、线段、方框、表格）；指标模板与图表布局保存 | 写进分析 02 与预警与扫描 02 的验收标准 |

---

## 7. 待核实

Pine 参考手册的 `ta.ema`、`ta.rma`、`ta.stdev`、`ta.tr`、`ta.atr`、`ta.vwap`、`ta.macd`、`ta.sma` 已在 2026-11-21 用浏览器打开公开页面逐字读取（[PINE-REF*]），结果写入 §5。

仍未核实：

- 内置 MACD 指标的默认参数（只核实了 `ta.macd` 示例用 12、26、9）。
- 帮助中心的图表快捷键、多图表同步与颜色联动组、K 线回放。这些只影响交互设计的细节，不影响指标数值，做第一批时再看。

---

## 来源

- **[AC-DRAW*]** Advanced Charts 文档，Drawings list：https://www.tradingview.com/charting-library-docs/latest/ui_elements/drawings/Drawings-List/
- **[AC-IND*]** Advanced Charts 文档，Indicators：https://www.tradingview.com/charting-library-docs/latest/ui_elements/indicators/ ；Indicators list：https://www.tradingview.com/charting-library-docs/latest/ui_elements/indicators/Indicators-List
- **[AC-OBJ*]** Advanced Charts 文档，Object tree：https://www.tradingview.com/charting-library-docs/latest/ui_elements/object-tree
- **[AC-SAVE*]** Advanced Charts 文档，Saving and loading：https://www.tradingview.com/charting-library-docs/latest/saving_loading/
- **[TV-MCL]** 见 [local-multi-frontend-product-form.md](./local-multi-frontend-product-form.md) §2.3 的 [TV-MCL]
- **[PINE-REF*]** Pine Script v6 参考手册（`#fun_ta.ema`、`#fun_ta.rma`、`#fun_ta.stdev`、`#fun_ta.tr`、`#var_ta.tr`、`#fun_ta.atr`、`#fun_ta.vwap`、`#fun_ta.macd`、`#fun_ta.sma` 各条目）：https://www.tradingview.com/pine-script-reference/v6/
- **[TV-SMA]** https://www.tradingview.com/support/solutions/43000696841-simple-moving-average/
- **[TV-EMA]** https://www.tradingview.com/support/solutions/43000592270-exponential-moving-average/
- **[TV-RSI]** https://www.tradingview.com/support/solutions/43000502338-relative-strength-index-rsi/
- **[TV-ATR]** https://www.tradingview.com/support/solutions/43000501823-average-true-range-atr/
- **[TV-BB]** https://www.tradingview.com/support/solutions/43000501840-bollinger-bands-bb/
- **[TV-VWAP]** https://www.tradingview.com/support/solutions/43000502018-volume-weighted-average-price-vwap/
- **[TV-MACD]** https://www.tradingview.com/support/solutions/43000502344-moving-average-convergence-divergence-macd-indicator/
- **[TV-STOCH]** https://www.tradingview.com/support/solutions/43000502332-stochastic-stoch/
- **[KUC*]** https://github.com/aztharion/klinechart-ui-core
- **[RKU*]** https://github.com/NemeZZiZZ/react-klinecharts-ui
- **[LTC*]** https://github.com/difurious/lightweight-charts-line-tools-core
- **[LCD*]** https://github.com/deepentropy/lightweight-charts-drawing
- **[MC-CS]** https://github.com/Mathieu2301/TradingView-API/blob/main/src/chart/chart-session.ts
- **[MC-QS]** https://github.com/Mathieu2301/TradingView-API/blob/main/src/quote/quote-session.ts
- **[MC-STUDY]** https://github.com/Mathieu2301/TradingView-API/blob/main/src/indicators/pine-indicator.ts
- **[MC-BUILTIN]** https://github.com/Mathieu2301/TradingView-API/blob/main/src/indicators/builtin-indicator.ts
- **[MC-GFX]** https://github.com/Mathieu2301/TradingView-API/blob/main/src/chart/graphics.ts
- **[MC-TF]** https://github.com/Mathieu2301/TradingView-API/blob/main/src/chart/timeframes.ts
