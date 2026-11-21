# 图表规则脚本（类 Pine）：执行语义、现成方案与实现路线

- 调研日期：2026-11-20。
- 范围：用户想在图表上写规则（类似 TradingView Pine Script）定义指标和信号，同一条规则还要被 Python 策略、回测和 agent 复用，且不能有前视。本文回答四个问题：
  1. Pine Script 的执行模型（逐柱执行、series、`[]` 历史引用、repainting、`barstate.isconfirmed`、`request.security` 的 lookahead），作为 Talaria 规则语义要对齐或避免的参照；
  2. 有官方仓库或文档可核实的类 Pine / 规则方案，以及 KLineChart v10 自定义指标机制、NautilusTrader / Backtrader / vectorbt 的指标定义方式；
  3. 四条实现路线（自定义 DSL、受限 Python、Pine 子集转译、JSON 表达式树）的前视防护、Decimal 一致性、agent 友好度、实现量与安全风险；
  4. TradingView 条款对“兼容 Pine 语法”的限制。
- 方法：只读官方文档、官方仓库源码（固定提交）、许可证文本和 GitHub 仓库元数据（`gh api`，读取于 2026-11-20）。**没有安装或运行任何外部项目。**每条结论标注来源，并区分：
  - **事实**：一手来源原文或源码直接写明；
  - **作者宣称**：项目方自述、未经本文独立验证的能力或测试结果；
  - **推断**：本文据事实做出的判断，包括实现量估计。
- 已有调研不重复：KLineChart 的选型、许可证与 Overlay 能力见 [copilot-trading-workbench-sources.md](copilot-trading-workbench-sources.md)（§3.2、[K1]–[K3]）；TA-Lib / talipp 的依赖核实见 [external-dependency-verification-v1.md](external-dependency-verification-v1.md)；NautilusTrader 执行面见 [agentic-trading-execution-sources.md](agentic-trading-execution-sources.md)。本文只补充 KLineChart 的 `registerIndicator` 契约和 Nautilus 的指标/公式部分。
- 许可证分析是工程合规核查，不构成法律意见。

## 1. 核心结论

1. **事实：Pine 的语义核心是“逐柱执行 + 已提交历史 + 实时柱回滚”。**历史柱上每根执行一次；实时柱上每次行情更新都重算，重算前把变量回滚到上次提交的状态，只有收盘那一次更新会被提交 [P1]。`x[1]` 引用上一根柱的已提交值，超出历史返回 `na` [P1][P5]。Talaria 的规则语义只需对齐“只用已闭合柱”这一部分，不必复制回滚和 `varip`（推断）。
2. **事实：Pine 官方把“历史与实时行为不一致”称为 repainting，并把未来信息泄漏列为不可接受。**`request.security` 默认 `lookahead_off`；`lookahead_on` 不加 `[1]` 偏移，会在历史柱上取到当时还未出现的高周期数据 [P3]。官方给出的不重绘写法是 `expression[1]` 加 `lookahead_on`，并说明两者缺一不可 [P2]。规则里需要跨周期时，Talaria 可以直接把这个写法定为唯一语义：只取最近一根已闭合的高周期柱（推断）。
3. **事实：现成的 Pine 兼容实现都不满足 Talaria 的“Decimal 单一权威”约束。**PineTS 是 AGPL-3.0 或商业许可的 JS/TS 运行时 [R1]；PyneCore 是 Apache-2.0 的 Python 运行时，`ta` 模块用 `float` 标注，`.pine` 源码要经过需要 API key 的外部服务 PyneComp 转换 [R2]；pynescript 只解析和反解析 Pine，不执行 [R3]。引入任何一个，都会出现第二套指标实现，与 ADR-0010 冲突（推断）。
4. **事实：KLineChart v10 的 `calc` 可以返回 Promise，结果按时间戳索引** [K1]。所以“规则在 Python 端求值，前端只渲染”在图表库层面不需要绕路。仓库现有 `MarketChart.tsx` 已用同步 `calc` 对齐后端 `IndicatorSeries`。
5. **推断：最匹配约束的路线是“声明式表达式树为规范形态（d），可选加一层薄文本语法（a）编译到同一棵树”。**前视防护能做成结构保证（树里不存在指向未来的节点），数值直接调用现有 Decimal 算子，agent 可以按 JSON Schema 生成，也没有执行任意代码的风险。代价是需要自己定义并维护语法和求值器。现有 `AlertCondition` 是扁平的单条件模型，可复用的是它的字段词汇和校验风格，不能直接当表达式树用（本地代码事实，见 §4.4）。

## 2. Pine Script 执行模型（v6 手册）

官方手册当前版本是 v6，示例使用 `//@version=6`；以下事实均出自 v6 手册，本文没有逐条核对 v5 的差异 [P1]。

| 主题 | 事实 | 来源 | 对 Talaria 的含义（推断） |
|---|---|---|---|
| 逐柱执行 | 脚本从第一根柱执行到最新一根；指标在每根历史柱上“execute once”。实时柱上，每收到一次数据更新就执行一次 | [P1] | 规则求值器以“柱”为单位推进，与现有算子 `_Engine.push(bar)` 的增量模型一致 |
| 实时回滚 | 实时柱每次重算前，变量、表达式和输出都回滚到上次提交的状态。`var` 变量同样回滚，`varip` 例外。收盘前各次更新的值不保存 | [P1] | Talaria 只在柱闭合后求值，就不需要回滚机制和 `varip` |
| 历史引用 `[]` | `x[1]` 是上一根柱的值。对局部变量，`[]` 返回“截至那根柱的最后提交值” | [P1] | 规则的历史引用只允许非负整数偏移，在全局序列上定义，避免局部变量的歧义 |
| 超出历史 | “`close[3]` will return na because no bar exists” | [P5] | 对应现有算子的“预热期为前导 `None`”（`compute_series` 的 docstring） |
| 负偏移 | 手册把偏移描述为“the offset in the past”，没有明确写负偏移是否合法 | [P5] | 未查到明确规定；Talaria 应在语法层直接拒绝负偏移 |
| 历史缓冲 | 每个序列都有历史缓冲，最多 5000 根；实时柱上引用超出缓冲会报运行时错误；`max_bars_back` 可预设缓冲大小 | [P1] | 现有 `MAX_PERIOD = 5000` 与此同量级，规则的最大回看可以用同一上限 |
| `barstate.isconfirmed` | 在所有历史柱和实时柱的最后一次（收盘）更新上为 true；在 `request.security()` 调用内部不起作用 | [P4] | Talaria“只在闭合柱求值”相当于每条规则都隐含 `and barstate.isconfirmed` |
| `barstate.isnew` | 历史柱上为 true；实时柱上只在第一次（开盘）更新时为 true | [P4] | 这正是 [P2] 所说的历史与实时不一致的来源之一；Talaria 不提供该语义 |
| Repainting 定义与分级 | 定义为历史与实时的计算或绘图行为不同。分为常见可接受、可能误导、不可接受（未来泄漏、实时盘中触发的预警/下单）、不可避免（数据修订等） | [P2] | “不可接受”这一级就是 Talaria 的硬约束 |
| 避免手段 | 信号条件加 `and barstate.isconfirmed`；用 `[1]` 或 `open`；默认不向过去绘图；不重绘的信号通常比重绘的晚到 | [P2] | “晚一根”是只用闭合柱的固有代价，产品上要明示 |
| `request.security` lookahead | 默认 `barmerge.lookahead_off`。`lookahead_on` 不加偏移时，历史柱上会提前显示高周期的最终值；这样泄漏未来的脚本不允许发布 | [P3][P2] | 跨周期引用只能取“最近一根已闭合的高周期柱” |
| 不重绘的 security 写法 | `request.security(sym, tf, expression[1], lookahead = barmerge.lookahead_on)`；官方说明偏移与 lookahead 互相依赖，去掉任何一个都会破坏函数 | [P2][P3] | 这一写法可以直接作为 Talaria 跨周期节点的唯一语义，不暴露 lookahead 开关 |
| `gaps` | 默认 `gaps_off`：历史柱用最近确认值填充，实时柱用最新的未确认值；`gaps_on` 在新数据未确认的柱上返回 `na` | [P3] | 实时柱的 `gaps_off` 会用未确认值，Talaria 只在闭合时求值即可避开 |

## 3. 现有类 Pine / 规则方案

元数据由 `gh api repos/<owner>/<repo>` 读取（2026-11-20），“最近提交”取默认分支的最新提交。

| 方案 | 形态 | 许可证（事实） | 维护状态（事实） | 能否在 Python 端执行 | Decimal | 来源 |
|---|---|---|---|---|---|---|
| **PineTS**（`LuxAlgo/PineTS`） | Pine v5/v6 转译器与运行时，跑在 Node.js 和浏览器；也接受 JS 风格语法 | AGPL-3.0，另附 `LICENSE-COMMERCIAL.md` 商业许可 | 最新发布 `v0.14.0`（2026-11-13）；最新提交 `7b6a48e`（2026-11-13） | 否，JS/TS 运行时 | 否（JS number）。README 宣称“matches TradingView's calculation precision”（作者宣称），并把原生 Pine v6 标为 experimental | [R1] |
| **PyneCore**（`PyneSys/pynecore`） | Python 运行时：在 import 时用 AST 变换，让带 `@pyne` 标记的 Python 代码按 Pine 的逐柱语义执行；包含全部 v6 `ta.*` 和策略回测 | Apache-2.0 | 最新发布 `v6.14.1`（2026-11-18）；最新提交 `04b6872`（2026-11-20） | 能，但写的是 Pyne 风格 Python，不是 Pine 源码。`.pine` 要经过 PyneComp 转换，那是“a separate PyneSys service that needs an API key” | 否：`lib/ta.py` 的序列类型标注为 `Series[float]`；`core/aggregator.py` 只在成交量聚合时临时用 `Decimal` 求和后转回 `float`（源码事实）。README 宣称在 1000 个公开脚本上与 TradingView 对比，99.771% 的绘图值逐位一致（作者宣称） | [R2] |
| **pynescript**（`elbakramer/pynescript`，PyPI `pynescript`） | Pine 解析器：把源码解析成 AST、dump、再反解析回 Pine | LGPL-3.0 | 最新发布 `v0.3.0`（2025-12-07）；最新提交 `0b9b4d0`（2025-12-18），近 11 个月无提交 | 只解析，不执行 | 不涉及 | [R3] |
| **KLineChart v10 自定义指标** | `registerIndicator({ name, calcParams, figures, calc, draw, ... })` 全局注册，之后通过 `createIndicator` 使用。无内置脚本语言，指标就是 TS/JS 函数 | Apache-2.0（见既有调研 [K1]） | 最新发布 `v10.1.1`（2026-11-16）；最新提交 `4a67cb9`；仓库锁定 `klinecharts: 10.1.1` | 否（前端） | 否。`calc` 接收 `KLineData[]`（数值字段） | [K1][K2] |
| **NautilusTrader 指标** | 指标对象经 `register_indicator_for_bars` 注册到策略，由 bar 驱动更新；行动前检查 `indicators_initialized()`。Rust 指标 trait 有 `handle_bar`、`update_raw(f64)`、`value() -> f64` | LGPL-3.0 | 最新提交 `0db3667`（2026-11-20，`develop`）；最新正式发布 `v1.231.0`（2026-08-02） | 能（Python 绑定） | 指标值是 `f64`；`Price`/`Quantity` 为定点数，部分运算返回 `Decimal` | [N1][N3][N4] |
| **NautilusTrader 合成品公式** | 声明式数值表达式语言：引用标的、四则运算、比较、逻辑、`if()`、局部赋值；编译期限制栈深 32、局部变量 16、嵌套深度 128 | 同上 | 同上 | 能（Rust 引擎） | 否：字面量“Evaluated with `f64` semantics” | [N2] |
| **Backtrader** | 在 `__init__` 中声明 line，在 `next()` 中逐柱读取；`[0]` 是当前值，`[-1]` 是上一根（负索引表示过去，与 Pine 的 `[1]` 方向相反） | GPL-3.0 | 最新提交 `b853d7c`（2023-04-19），GitHub 上无 release | 能 | 文档未涉及 Decimal（未核实） | [B1] |
| **vectorbt** | `IndicatorFactory(input_names, param_names, output_names)` 配合 `from_apply_func` / `from_custom_func`；也可包装 TA-Lib、pandas-ta、ta | Apache-2.0 + Commons Clause（禁止“Sell”） | 最新发布 `v1.2.1`（2026-11-11）；最新提交 `9ac1b69` | 能 | 否：NumPy/Numba，默认 `np.float64`。文档未讨论前视 | [V1] |
| **JsonLogic** | 以 JSON 表达规则，`{"op": [args]}`；声明“只读所提供的数据”、不使用 `eval()`、无副作用、执行时间确定；可以添加自定义运算符 | （未核实许可证） | 未核实 | 有多语言实现（未核实具体实现） | 规范本身不涉及 | [J1] |

补充说明：

- **事实：PineTS 的 JS 风格语法需要显式信任模式。**README 写明，用 JS 函数写的指标需要开启 trust mode，`run()` 默认只接受带版本号的 Pine [R1]。也就是说，作者自己也把“执行 JS”和“执行 Pine”区分为两种信任等级。
- **推断：AGPL-3.0 的义务主要在分发或通过网络向他人提供服务时触发**，Talaria 是本机单用户，目前风险不大。但它是 JS 运行时，数值是 float，与 ADR-0010 冲突，这一点比许可证更决定取舍。
- 搜索中出现了 `xtoor/pine2py`、OpenPineScript、`Opus-Aether-AI/pine-transpiler` 等仓库，本文没有核查其源码与维护状态，不列入比较。

## 4. 实现路线对比

### 4.0 共同前提（本地代码事实）

- 算子核：`core/src/talaria/foundation/operators/core.py` 的 `compute_series(bars, spec)` 和 `update_series(state, new_bars, spec)` 分别做批量和增量计算，每个指标是一个 `push(bar) -> Point` 引擎。docstring 写明增量结果与批量逐值一致，预热期为前导 `None`。指标集合为 `IndicatorName` 枚举，包括 EMA/SMA/RSI/ATR/BOLL/VWAP 和 3 个衍生品指标；通道由 `CHANNELS` 定义。
- 预警契约：`core/src/talaria/foundation/alerts/contracts.py` 的 `AlertCondition` 是**单条扁平条件**，按 `kind` 区分 `METRIC_THRESHOLD / INDICATOR_SIGNAL / OVERLAY_CROSS / LEVEL_EVENT`，由 `model_validator` 校验字段组合；没有 AND/OR 组合，也没有算术、历史偏移或嵌套。`AlertOverlayMode.CLOSE_BREAK`（收盘确认突破）和 `AlertLevelEvent.BREAK` 说明现有契约已经区分“盘中触碰”和“收盘确认”。
- 前端：`dsh/packages/talaria-desk-ui/src/features/market-chart/MarketChart.tsx` 注册了两个空模板指标（主图与副图），`calc` 只把后端 `IndicatorSeries` 对齐到 K 线，前端不计算指标。
- KLineChart v10 的 `calc` 签名为 `(kLineDataList, indicator) => Record<Timestamp, unknown> | Promise<Record<Timestamp, unknown>>` [K1]，适合异步拉取后端的求值结果。
- Python `decimal` 的上下文（精度、舍入、traps）对每个线程独立；默认构建下，C 实现使用协程局部的上下文 [S4]。规则求值器无论走哪条路线，都要在固定的 `localcontext` 中运行，才能与黄金向量的精度和舍入一致（推断）。

### 4.1 对比表

| 维度 | a. 自定义受限 DSL → Python 解析 | b. 受限 Python 子集（沙箱） | c. 兼容 Pine 语法子集（转译到 Python 算子） | d. JSON/YAML 声明式表达式树 |
|---|---|---|---|---|
| **前视防护** | 语法层保证：只提供非负偏移 `x[n]`，没有“未来”语法；跨周期只提供 [P2] 那种“最近已闭合高周期柱”的节点；求值器只接收闭合柱（推断） | 难以从结构上保证：用户代码拿到序列对象后可能做任意下标运算，只能靠只暴露“截至当前柱”的视图对象，再加运行期检查（推断） | 必须改变 Pine 原语义：`lookahead_on`、`barstate.isnew`、`varip`、`timenow` 等要么拒绝，要么改义 [P1]–[P4]。“像 Pine 但行为不同”反而容易误导（推断） | 与 a 相同，且更直接：节点类型白名单里不存在负偏移或未来引用，Schema 校验即可拦下（推断） |
| **Decimal 一致性** | 字面量按 `Decimal(str)` 解析，指标节点直接调用 `compute_series`/`update_series`，与黄金向量同源（推断） | 用户代码里的 `float` 字面量、`/`、`math.*` 会悄悄引入浮点；要么在 AST 层改写字面量，要么只给 Decimal 包装类型（推断）。Monty 的标准库列表含 `decimal`，但有自己的差异页，本文未核实完整度 [S2] | Pine 只有 `float`/`int`，其精度语义以 TradingView 为准；转译到 Decimal 后，数值一定与 TradingView 有差异，“兼容”只能是语法兼容（推断） | 与 a 相同 |
| **agent 生成友好度** | 中：需要把语法文档放进提示词；可以提供“解析 + 报错位置”的校验工具让 agent 自我修正（推断） | 高：模型最熟悉 Python。但生成的代码可能用到白名单外的写法，失败率取决于白名单宽度（推断） | 高（模型见过大量 Pine），但模型会写出子集外的特性（`request.*`、`strategy.*`、`var`/`varip`、UDT），需要大量“不支持”报错（推断） | 高：可直接给 agent JSON Schema 或结构化输出；pydantic 校验与现有契约风格一致。人手写较啰嗦（推断） |
| **实现量估计（推断）** | 中：语法与解析器（如手写 Pratt 解析器）、类型检查、求值器、错误定位、前端编辑器高亮。粗估 2–3 周 | 中到高：自写 AST 白名单约 1–2 周，但安全审计是长期成本；接 Monty 等外部沙箱可以少写代码，但新增 Rust 扩展依赖 | 高：Pine 的类型限定（`const/input/simple/series`）、`na` 传播、函数各自的状态、`ta.*` 语义逐个对齐；只做表达式子集也要 3–6 周，且持续追 Pine 版本 | 低到中：pydantic 判别联合 + 递归求值器 + 少量组合子（cross、and/or、偏移、跨周期），粗估 1–2 周；文本语法可以以后再加 |
| **安全风险** | 低：没有通用执行能力；需要限制嵌套深度和回看长度，防止资源耗尽（Nautilus 公式引擎的编译期上限可作参照 [N2]） | 高：RestrictedPython 自述“not a sandbox system or a secured environment” [S1]；其安全文档说明守卫只覆盖受限源码中显式写出的属性访问，“exposing an object exposes everything reachable through it” [S1]；9.0 版刚从 `safe_builtins` 移除 `sorted`/`tuple`/`zip` 等绕过迭代守卫的内建 [S1]。Python 文档也警告 `ast.parse` 可能被足够大或复杂的输入因栈深度弄崩解释器 [S3] | 低到中（同 a），另有条款风险，见 §5 | 最低：数据不是代码；JsonLogic 的设计目标就是只读、无 `eval`、执行时间确定 [J1]。仍需深度和大小上限 |
| **与现有契约复用** | 可以编译到 d 的树 | 与 d 无关 | 可以转译到 d 的树 | `AlertCondition` 的枚举（`AlertDirection`、`IndicatorName`、`CHANNELS`）和 `model_validator` 风格可以直接沿用；`AlertCreate.condition` 将来可以换成树根，或新增 `kind = RULE_EXPR`（推断） |

### 4.2 路线 b 的补充事实

- RestrictedPython 支持 CPython 3.10–3.15，不支持 PyPy [S1]；9.0（2026-11-04）不再支持 3.10，并新增“Security considerations”文档页 [S1]。许可证在 GitHub 显示为 NOASSERTION（ZPL），本文未读许可证全文。
- RestrictedPython 默认不提供 `__import__`；一旦提供，“Every module restricted code can then import becomes part of your trusted computing base” [S1]。
- Monty（`pydantic/monty`，MIT，最新发布 `v1.6.0`（2026-11-11），最新提交 `63ae691`）是用 Rust 写的 Python 子集解释器，自称面向“code written by AI”；沙箱内不存在文件系统、环境变量和网络，只能通过宿主传入的函数访问宿主（作者宣称）[S2]。限制：不支持第三方包；类不能用方法装饰器和元类 [S2]。
- 推断：b 的真正问题不只在安全。Python 策略本来就能直接调用算子；“图表规则”如果也用 Python，就和“Python 策略”成了同一物种，失去了可审计、可静态检查前视的中间形态。

### 4.3 路线 c 的补充事实

- Pine 的值有 `na`、`var`/`varip` 持久化、实时回滚、局部变量 `[]` 的特殊语义 [P1]，`request.security` 有 lookahead 与 gaps 两组开关 [P3]。这些是 c 必须逐一决定“支持、拒绝还是改义”的面。
- PyneCore 的做法是把 Pine 语义搬进 Python（AST 变换，加 `Series`/`Persistent` 注解），但 `.pine` → Python 的转换器 PyneComp 不开源，需要 API key [R2]。pynescript 提供的是开源的 Pine 解析器（LGPL-3.0），近 11 个月无提交 [R3]。

### 4.4 路线 d 与现有预警契约

- 事实：`AlertCondition` 的 `INDICATOR_SIGNAL` 已经表达了“指标与阈值比较”或“指标与另一指标比较”，`direction` 为 `ABOVE/BELOW`；`OVERLAY_CROSS` 有 `TOUCH/CLOSE_BREAK` 两种判定。
- 推断：可以把它看成表达式树的一个特例（`Compare(Indicator, Const|Indicator)`），树里再补：
  - 组合节点：`all` / `any` / `not`；
  - 历史偏移：`prev(x, n)`，n ≥ 0；
  - 穿越：`cross_above(a, b)`，等价于 `a > b and prev(a,1) <= prev(b,1)`；
  - 算术：Decimal 四则运算；
  - 跨周期：`htf(tf, x)`，固定为最近已闭合柱的语义；
  - 输出声明：作为指标绘制，或作为信号/预警。
- 推断：预警引擎、回测和图表共用同一个“树 → 逐柱求值”的函数。它内部按 `IndicatorSpec` 去重，再调用 `compute_series`/`update_series`，图表就和策略拿到同一口径的数据。

## 5. TradingView 条款与“兼容 Pine 语法”

- **事实（TradingView Terms of Use [T1]）：**
  - §12(b) 禁止“reverse engineer, decompile, disassemble, decrypt”或以其他方式推导源码，但适用法律另有明确允许的除外。
  - §12(d) 禁止使用 TradingView 或其服务“to build, develop, or improve a product or service that competes with TradingView”，包括复制其功能或界面。
  - §22 规定，脚本未声明许可证时默认按 Mozilla Public License 2.0 授权；发布脚本即授予 TradingView 永久、不可撤销的许可。
  - §4 规定，在 TradingView 网站之外使用其产品需要署名。
  - 页面没有标注最后更新日期。
- **事实：**条款页没有专门针对“Pine Script”名称或语法的商标条款 [T1]。本文没有找到 TradingView 官方的商标或品牌使用指南页面。PineTS 和 PyneCore 都在 README 中声明与 TradingView 无隶属关系，且 Pine Script 是 TradingView 的商标 [R1][R2]。这是第三方的自我声明，不是 TradingView 的授权。
- **推断：**
  - 条款约束的是“使用 TradingView 服务的人”。Talaria 如果只是自写解析器、不访问 TradingView 服务，§12(d) 的适用性不明确。但开发过程中如果参考 TradingView 平台上的内容（例如抓取或复制已发布脚本、对照 TradingView 输出调参），就进入了条款覆盖的范围。
  - 自定义 DSL 或表达式树（a/d）不涉及这一风险；“兼容 Pine”（c）至少应避免使用 Pine 名称作为产品功能名，也不应批量导入 TradingView 上的社区脚本（MPL-2.0 或作者自定许可，需要逐个看）。
  - 这不是法律意见。

## 6. 对 Talaria 的启示

以下是可选路线及取舍，不替用户做最终决定。

1. **路线一：表达式树为规范形态（d），后续可加薄文本语法（a）。**
   - 做法：规则以 pydantic 判别联合存储，节点白名单只含非负偏移、闭合柱求值和“最近已闭合高周期柱”这类跨周期语义；求值器调用 `compute_series`/`update_series`；前端通过 KLineChart 异步 `calc` 或现有 `IndicatorSeries` 通道渲染结果。agent 直接产出 JSON，人以后可以用文本语法或表单编辑。
   - 好处：前视是结构保证；Decimal 与黄金向量同源；`AlertCondition` 的词汇可以沿用；安全面最小；实现量最小（推断 1–2 周）。
   - 代价：人手写 JSON 不如 Pine 顺手，体验依赖表单或后续的文本语法；表达力受节点集合限制，循环、自定义状态机做不了。
2. **路线二：Pine 风格的表达式子集，转译到同一棵树（c 的收窄版）。**
   - 做法：只接受 Pine 的表达式部分（`ta.ema(close, 20) > ta.sma(close, 50)`、`x[1]`、`ta.crossover`、`and/or`），解析后编译成路线一的树。不支持 `var/varip/request.*/strategy.*`，也不使用“Pine”名称。
   - 好处：用户和 agent 的肌肉记忆可用。
   - 代价：数值只能做到语法兼容，结果与 TradingView 必然不同，容易让人误以为能直接粘贴社区脚本；解析器、报错、版本追踪的长期成本高于路线一（推断 3–6 周）；条款上有灰区（§5）。
3. **路线三：图表规则不单独设计，直接用受限 Python（b）。**
   - 做法：规则就是一段受限 Python，靠 AST 白名单或 Monty 一类沙箱运行，只暴露 Decimal 序列视图和算子函数。
   - 好处：与“Python 策略”同一种写法，agent 最熟悉，表达力最强。
   - 代价：前视只能靠运行期视图对象防护，不能静态证明；RestrictedPython 自述不是沙箱，安全边界取决于暴露对象的审计 [S1]；浮点字面量会悄悄破坏 Decimal 一致性；图表上的“规则”与 Python 策略的边界变得模糊。

无论选哪条，都建议先把两条语义写进 ADR：

- 规则只在闭合柱上求值，相当于每条规则都隐含 Pine 的 `barstate.isconfirmed` [P4]；
- 跨周期引用固定为 [P2] 的 `expression[1] + lookahead_on` 语义。

并为规则求值器补一组像 `contracts/operator_vectors/` 那样的黄金向量，覆盖穿越、偏移和跨周期对齐（推断）。

## 7. 未查到一手资料的问题

- Pine 历史引用的**负偏移**是否合法：v6 手册的执行模型页和运算符页都没有明确写 [P1][P5]。
- TradingView 对第三方使用“Pine Script”名称、实现兼容语法的**专门商标或品牌政策**：没有找到官方页面；条款页没有专门条款 [T1]。
- Monty 的 `decimal` 模块完整度：限制页只说列入、另有差异页，本文没有读差异页 [S2]。
- Backtrader 的 `minperiod`/`prenext` 预热语义和 Decimal 支持：所读概念页没有涉及 [B1]。
- JsonLogic 的许可证与 Python 实现的维护状态：没有核实 [J1]。
- PineTS 的 Pine v6 覆盖率细节（README 只有覆盖率徽章）[R1]。
- v5 与 v6 在执行模型上的差异：本文只读了 v6 手册。

## 来源

### TradingView 官方（读取于 2026-11-20）

- [P1] Pine Script v6 手册：Execution model（含 Time series、历史缓冲、`max_bars_back`）：https://www.tradingview.com/pine-script-docs/language/execution-model/
- [P2] Repainting：https://www.tradingview.com/pine-script-docs/concepts/repainting/
- [P3] Other timeframes and data（`request.security` 的 lookahead/gaps）：https://www.tradingview.com/pine-script-docs/concepts/other-timeframes-and-data/
- [P4] Bar states：https://www.tradingview.com/pine-script-docs/concepts/bar-states/
- [P5] Operators（历史引用运算符）：https://www.tradingview.com/pine-script-docs/language/operators/
- [T1] Terms of Use, Policies and Disclaimers（§3、§4、§12(b)(d)、§22）：https://www.tradingview.com/policies/

### 类 Pine 实现

- [R1] PineTS：仓库 https://github.com/LuxAlgo/PineTS ；README 与许可证于提交 [`7b6a48e1abbb3a15768fdb7978cec09fb3f6e299`](https://github.com/LuxAlgo/PineTS/tree/7b6a48e1abbb3a15768fdb7978cec09fb3f6e299)（[LICENSE](https://github.com/LuxAlgo/PineTS/blob/7b6a48e1abbb3a15768fdb7978cec09fb3f6e299/LICENSE)、[LICENSE-COMMERCIAL.md](https://github.com/LuxAlgo/PineTS/blob/7b6a48e1abbb3a15768fdb7978cec09fb3f6e299/LICENSE-COMMERCIAL.md)）；发布 `v0.14.0`。
- [R2] PyneCore：仓库 https://github.com/PyneSys/pynecore ；提交 [`04b68723fcb99dd59e33cf2227d0e859010792f4`](https://github.com/PyneSys/pynecore/tree/04b68723fcb99dd59e33cf2227d0e859010792f4) 的 [README.md](https://github.com/PyneSys/pynecore/blob/04b68723fcb99dd59e33cf2227d0e859010792f4/README.md)、[src/pynecore/lib/ta.py](https://github.com/PyneSys/pynecore/blob/04b68723fcb99dd59e33cf2227d0e859010792f4/src/pynecore/lib/ta.py)、[src/pynecore/core/aggregator.py](https://github.com/PyneSys/pynecore/blob/04b68723fcb99dd59e33cf2227d0e859010792f4/src/pynecore/core/aggregator.py)；发布 `v6.14.1`。
- [R3] pynescript：仓库 https://github.com/elbakramer/pynescript ；提交 [`0b9b4d0b0cd40d7d98c939830ccd06527a90c4d5`](https://github.com/elbakramer/pynescript/tree/0b9b4d0b0cd40d7d98c939830ccd06527a90c4d5) 的 README；文档 https://pynescript.readthedocs.io/ ；发布 `v0.3.0`。

### 图表与回测框架

- [K1] KLineChart `registerIndicator` API（10.1.1 文档）：https://klinecharts.com/en-US/api/chart/registerIndicator ；仓库 https://github.com/klinecharts/KLineChart 提交 `4a67cb9c0ac5395b0f0c65197ba5c1230b09cda0`，发布 `v10.1.1`。
- [K2] KLineChart 自定义指标指南：https://klinecharts.com/en-US/guide/indicator
- [N1] NautilusTrader strategies.md（`register_indicator_for_bars`、`indicators_initialized()`）：https://github.com/nautechsystems/nautilus_trader/blob/0db3667a3c7b13e01f12a9bf446616c571f1fce0/docs/concepts/strategies.md
- [N2] NautilusTrader synthetics.md（公式语言、`f64` 语义、编译期限制）：https://github.com/nautechsystems/nautilus_trader/blob/0db3667a3c7b13e01f12a9bf446616c571f1fce0/docs/concepts/synthetics.md
- [N3] NautilusTrader 指标源码 [crates/indicators/src/indicator.rs](https://github.com/nautechsystems/nautilus_trader/blob/0db3667a3c7b13e01f12a9bf446616c571f1fce0/crates/indicators/src/indicator.rs)、[crates/indicators/src/average/ema.rs](https://github.com/nautechsystems/nautilus_trader/blob/0db3667a3c7b13e01f12a9bf446616c571f1fce0/crates/indicators/src/average/ema.rs)
- [N4] NautilusTrader value_types.md（定点数 `Price`/`Quantity` 与 `Decimal` 返回）：https://github.com/nautechsystems/nautilus_trader/blob/0db3667a3c7b13e01f12a9bf446616c571f1fce0/docs/concepts/value_types.md
- [B1] Backtrader Platform Concepts：https://www.backtrader.com/docu/concepts/ ；仓库 https://github.com/mementum/backtrader （GPL-3.0，最新提交 `b853d7c90b6721476eb5a5ea3135224e33db1f14`，2023-04-19）
- [V1] vectorbt IndicatorFactory：https://vectorbt.dev/api/indicators/factory/ ；[LICENSE.md（Apache 2.0 + Commons Clause）](https://github.com/polakowo/vectorbt/blob/9ac1b697166b00911def494cf95937274218aa54/LICENSE.md)，发布 `v1.2.1`。
- [J1] JsonLogic：https://jsonlogic.com/

### 沙箱与 Python 官方

- [S1] RestrictedPython 文档 https://restrictedpython.readthedocs.io/en/latest/ ；[docs/usage/security_considerations.rst](https://github.com/zopefoundation/RestrictedPython/blob/9d7aeb267e598c3eb270850c223b343ca5a5d2cb/docs/usage/security_considerations.rst)；[CHANGES.rst（9.0，2026-11-04）](https://github.com/zopefoundation/RestrictedPython/blob/9d7aeb267e598c3eb270850c223b343ca5a5d2cb/CHANGES.rst)
- [S2] Monty：[README](https://github.com/pydantic/monty/blob/63ae6919c0dc2db89d4bd69268276673ad8bf3b7/README.md)；限制说明 https://pydantic.dev/docs/monty/limitations/ ；发布 `v1.6.0`。
- [S3] Python `ast` 文档（`ast.parse` 与 `literal_eval` 的警告）：https://docs.python.org/3/library/ast.html
- [S4] Python `decimal` 文档（上下文与线程/协程局部）：https://docs.python.org/3/library/decimal.html

### 本地代码（仓库当前 `main`，`45e1c95`）

- `core/src/talaria/foundation/operators/core.py`、`core/src/talaria/foundation/operators/spec.py`
- `core/src/talaria/foundation/alerts/contracts.py`
- `dsh/packages/talaria-desk-ui/src/features/market-chart/MarketChart.tsx`
- `docs/adr/0010-pure-python-indicator-core.md`
