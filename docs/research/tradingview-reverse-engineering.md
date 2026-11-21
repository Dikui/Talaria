# TradingView 逆向与非官方项目：能用什么、不能用什么

- **调研日期**：2026-11-21（Asia/Shanghai）。仓库 star 数、最近提交时间和许可证均为当日通过 GitHub REST API 读取；PyPI / npm 版本为当日从官方 registry 读取。
- **范围**：用户想“直接复刻 TradingView 的图表和指标”。本文回答四件事：
  1. 公开的逆向或非官方项目有哪些，分四类：a. 非官方行情 / 指标接口；b. 泄露或再分发的 `charting_library`（Advanced Charts）；c. Pine Script 解析器、转译器和运行时；d. 仿 TradingView 界面的开源前端。
  2. 每个项目的仓库、活跃度、star 数量级、许可证、实现方式、维护状态和已知问题。
  3. TradingView 服务条款、Advanced Charts 许可表态、交易所行情再分发限制的原文要点。
  4. 对 Talaria 的结论：哪些能用、哪些不能用，以及与 ADR-0010（指标单一权威实现）的冲突。
- **方法**：只读项目 README、源码文件、`package.json`、GitHub issue、TradingView 官方文档与 Terms of Use、GitHub 官方 `github/dmca` 仓库、Nasdaq 官方数据政策 PDF。**没有下载、克隆、安装或运行任何项目；没有连接 TradingView 的任何接口；对疑似再分发 `charting_library` 的 npm 包只读了 registry 的元数据（名称、描述、日期），没有下载包内容。**
- **标注约定**：**事实**＝来源原文可直接核对；**项目自述**＝项目对自身能力或测试结果的说法，未独立验证；**推断**＝面向 Talaria 的判断，不是来源结论。本文不是法律意见。
- **与已有调研的关系**：
  - 图表库选型（KLineChart v10 vs Lightweight Charts v5，许可与 Overlay 能力）见 [copilot-trading-workbench-sources.md](./copilot-trading-workbench-sources.md) §3.2，本文不重复。
  - Pine 执行模型、PineTS / PyneCore / pynescript 三个实现的许可与 Decimal 冲突、ToU §4/§12(b)(d)/§22 与“兼容 Pine 语法”的关系，见 [chart-rule-scripting-sources.md](./chart-rule-scripting-sources.md) §2、§3、§5。本文只补充该文未核查的 Pine 项目和条款新增部分（§12(a)(c)(e)、§3 非显示使用、§27 MCP Server）。
  - Advanced Charts 的 Datafeed 解耦方式见 [local-multi-frontend-product-form.md](./local-multi-frontend-product-form.md) §2.3。

---

## 1. 核心结论

1. **非官方接口都靠模拟浏览器访问 TradingView 的私有 WebSocket 或未公开 HTTP 端点，TradingView 条款明确禁止这种做法。** 最完整的是 `Mathieu2301/TradingView-API`（约 5.9k star，2026-11-14 仍在提交），自述能拿到实时 K 线、报价、内置与 Pine 指标值、策略回测报告、自选列表和图表画线 [MC-README]；它连的是 `wss://<server>.tradingview.com/socket.io/websocket`，并伪造浏览器 Origin [MC-CLIENT]。ToU §12(a) 禁止用自动化手段访问“internal or undocumented endpoints or real-time data feeds”，§12(e) 禁止开发或分发这类工具 [TOU]。已知问题包括封号 [MC-I179]、登录要求人机验证 [TVDF-I62]、接口突然失效 [TVDF-I63]。
2. **TradingView 现在有官方 MCP Server（公测），它是唯一合规的程序化通道，但条款把它限定为个人、仅供显示、不得存储、不得用于自动交易。** 它能返回 OHLCV、筛选器字段、技术评级，以及 Pine 指标的 plot 值和回测报告 [TV-MCP]；ToU §27.3 禁止系统性缓存和存储数据，也禁止用于自动交易或下单 [TOU]。所以即使走官方通道，Talaria 也不能把这些数据存进冷存储、喂给策略或预警。
3. **泄露的 Advanced Charts 代码不能用，TradingView 的态度一贯且明确。** 官方文档写明库不可再分发，禁止把库的任何部分放进公开仓库，也禁止从第三方渠道获取 [TV-QS][TV-UPD]；官方不向个人、爱好、学习或测试用途提供该库 [TV-FREE]。`github/dmca` 里有 2016–2019 年 TradingView 针对 `charting_library` 镜像的 7 份删除通知 [DMCA-2016][DMCA-2018-08][DMCA-2019]。npm 上仍能搜到描述里自称包含 Charting Library 的包（未下载核实）[NPM-SEARCH]。
4. **用 TradingView 服务端算出的指标值，与 ADR-0010 直接冲突。** ADR-0010 规定指标只由 Python `Decimal` 实现计算一次，图表、预警和智能体都用这一份结果，并明确否决“前端或 JS 库计算、Python 只转发” [ADR-0010]。TradingView 的指标值由第三方服务计算，算法不公开，条款禁止存储，也禁止用于非显示用途 [TOU]，所以无法纳入黄金向量，也不能给策略和预警用（推断）。
5. **能合法借鉴的是开源仿制前端的交互和代码，而不是 TradingView 本身。** 和 Talaria 现有 KLineChart v10.1.1 同栈的有 `aztharion/klinechart-ui-core`（Apache-2.0）和 `NemeZZiZZ/react-klinecharts-ui`（LICENSE 文件为 Apache-2.0）；画线交互可参考 `difurious/lightweight-charts-line-tools-core`（MPL-2.0）和 `deepentropy/lightweight-charts-drawing`（MIT）[KUC][RKU][LTC][LCD]。但 ToU §12(d) 禁止“使用 TradingView 服务”去复制其功能或界面 [TOU]，所以借鉴时应以这些开源项目为参照，不要对着 TradingView 页面逐项照抄（推断）。

---

## 2. 类别 a：非官方行情与指标接口

### 2.1 项目一览（事实，读取于 2026-11-21）

| 项目 | star | 最近提交 | 许可证 | 实现方式 | 能拿到什么（项目自述） | 维护状态 |
|---|---|---|---|---|---|---|
| **`Mathieu2301/TradingView-API`**（npm `@mathieuc/tradingview` 4.1.2，2026-11-14） | 约 5.9k | `2740ed9`，2026-11-14 | GitHub 未识别 LICENSE 文件；`package.json` 声明 ISC [MC-PKG] | 逆向私有 WebSocket：`wss://${server}.tradingview.com/socket.io/websocket?from=chart&type=chart`，Origin 设为 `https://www.tradingview.com` [MC-CLIENT]；自写帧编解码与压缩载荷解析 [MC-COV] | 实时 K 线与报价；内置与 Pine 指标值（有权限时含 invite-only）；策略报告与回测；回放；筛选器；账号的自选列表和**图表画线**；技术评级 [MC-README] | 活跃。v4 为 TypeScript 重写，2026-10-02/03 有一轮在线测试记录 [MC-COV] |
| **`rongardF/tvdatafeed`**（StreamAlpha 原项目的分叉） | 约 690 | `e6f6aaa`，2022-09-13 | MIT | 用户名密码 POST 到 `/accounts/signin/` 换取 token，再连 `wss://data.tradingview.com/socket.io/websocket` [TVDF-SRC] | 历史 K 线，单次最多 5000 根；分叉版增加实时推送 [TVDF-README] | 近三年无提交。原仓库 `StreamAlpha/tvdatafeed` 现返回 404；PyPI 上查不到 `tvdatafeed` 包 |
| **`AnalyzerREST/python-tradingview-ta`**（PyPI `tradingview-ta` 3.3.0，2022-10-05） | 约 1.3k | `4a11382`，2023-04-29 | MIT | 调用 `https://scanner.tradingview.com/` 的筛选器端点 [TA-SRC] | 内置指标的汇总评级和数值；**明确不支持 Pine / 自定义指标** [TA-README] | **已归档（archived）** |
| **`shner-elmo/TradingView-Screener`**（PyPI `tradingview-screener` 3.2.2，2026-09-14） | 约 1.4k | `5422d90`，2026-09-14 | MIT | 同样走筛选器端点；README 称之为“TradingView's official API”，但 TradingView 没有公开文档（推断：此说法是项目自述）；实时数据需要从本机浏览器读取会话 cookie [TVS-README] | 3000 多个字段：OHLC、技术指标、基本面；1m–1mo 多周期 [TVS-README] | 活跃 |
| **`mnwato/tradingview-scraper`**（PyPI `tradingview-scraper` 0.4.21，2026-09-18） | 约 460 | `68ea546`，2026-09-18 | MIT | 抓取网页（观点、新闻、概览、日历等）+ WebSocket；README 教用户复制浏览器 cookie 以“绕过验证码” [MNW-README] | 观点、新闻、技术面、市场列表；通过 WebSocket 拿 OHLC 和指标值 [MNW-README] | 活跃 |
| **`smitkunpara/tv-scraper`**（PyPI `tv-scraper` 1.5.1，2026-05-13） | 约 50 | `9a823af`，2026-09-23 | MIT | WebSocket + 网页抓取 [TVSC-README] | K 线加内置或自定义 Pine 指标流、实时价格、基本面、期权链、日历；还能**管理 Pine 脚本**（校验、创建、编辑、删除）[TVSC-README] | 活跃 |
| **`vnquants/tradingview-rs`**（PyPI `tradingview-rs` 0.4.5，2026-11-08） | 约 80 | `5794fe4`，2026-11-21 | MIT | Rust 实现，带 Python 绑定；自述受 Mathieu2301 项目启发；登录相关请求用“browser emulation”（浏览器伪装）[TVRS-README] | 历史与实时 K 线、基本面 Pine study、经济日历 [TVRS-README] | 活跃 |
| **`tradesdontlie/tradingview-mcp`** | 约 7.3k | `c05b8f5`，2026-07-28 | GitHub 显示 NOASSERTION；LICENSE 文件为 MIT | 不连 TradingView 服务器，而是用 `--remote-debugging-port=9222` 启动本机 TradingView 桌面版，通过 Chrome DevTools Protocol 读取和操作图表 [TDL-README] | 读取图表上的指标输出、编辑 Pine、画线、回放（项目自述）[TDL-README] | 活跃。README 自己写明可能与 ToU 冲突，数据不得再分发 [TDL-README] |
| **`atilaahmettaner/tradingview-mcp`** | 约 5.3k | `456ba41`，2026-11-20 | MIT | 自述不登录、不抓取 TradingView 会话；数据来自“TradingView screener provider”和 Yahoo Finance [ATA-README] | 筛选器、多周期技术分析、回测；作者另有收费托管版 [ATA-README] | 活跃 |
| **`jchao01/TradingView-data-scraper`** | 约 580 | `b585298`，2019-11-13 | Apache-2.0 | 解析用户**已公开发布**的图表页面，从中提取价格和指标数据 [JCH-README] | 已发布图表中的价格与指标 | 停更约 7 年 |
| **`imxeno/tradingview-scraper`**（Node.js） | 约 300 | `018df59`，2024-03-29 | ISC | 作者自称“implementation is a little bit dirty”[IMX-README] | 报价 | 停更 |

GitHub 搜索里还有大量同类小项目（Go、Dart、Rust 等语言的移植，以及多个 `tvdatafeed` 分叉），本文没有逐个核查。

### 2.2 “服务端计算的指标值”到底怎么来（事实）

- **内置指标**：`tradingview-ta` 和 `TradingView-Screener` 读的是筛选器端点上预先算好的字段（例如 RSI、MACD、均线评级），只有最新值或汇总值，没有完整序列 [TA-README][TVS-README]。
- **完整指标序列和 Pine 指标**：`TradingView-API`、`tv-scraper` 和 `tradingview-scraper` 在 WebSocket 会话里给图表挂上 study（内置或 Pine），由 TradingView 服务端逐柱计算后推回 plot 值 [MC-README][TVSC-README][MNW-README]。`TradingView-API` 文档写明，不登录时“no Pine studies”，历史更短，行情可能延迟或被替换 [MC-GS]。
- **画线**：只有 `TradingView-API`（读取账号图表上的画线）[MC-README] 和 `tradesdontlie/tradingview-mcp`（通过 CDP 操作桌面版）[TDL-README] 自述支持。

### 2.3 已知问题（事实）

- **封号**：`TradingView-API` issue #179（2023-03-02，至今 open）中，用户在约 6000 组参数上批量回测私有策略，遇到“Calculation timed out”和账号被封 [MC-I179]。
- **登录被人机验证拦截**：`tvdatafeed` issue #62（2024-12-07）显示，登录接口返回 `recaptcha_required` [TVDF-I62]；#83（2026-03-08）仍在修登录失败。
- **接口突然不可用**：`tvdatafeed` issue #63（2024-12-11），`get_hist()` 突然超时、拿不到数据 [TVDF-I63]。
- **协议随 TradingView 改版而变化**：`tradesdontlie/tradingview-mcp` 提醒，依赖的桌面版内部结构“can change or break without notice”，建议锁定桌面版版本 [TDL-README]。`TradingView-API` 曾因 Bun 运行时需要补 Origin 头才连得上 [MC-COV]。
- **账号凭据暴露**：多数项目需要用户提供 `sessionid` 等 cookie 或账号密码 [MC-GS][TVDF-README][TVS-README]。

### 2.4 官方通道：TradingView MCP Server（公测）（事实）

ToU §12(a)(ii) 只放行“official TradingView APIs or other interfaces that we expressly make available” [TOU]。目前能查到的官方程序化通道是 MCP Server：

- 地址 `https://mcp.tradingview.com/mcp`，用 OAuth 2.1 登录；需要付费套餐，每日调用次数按套餐计（Essential 每天 10 次，Ultimate 每天 500 次）[TV-MCP]。
- 工具共 38 个：`get_ohlcv`（单次最多 5000 行）、筛选器与技术评级、新闻、财报文件、经济日历、预警增删改，以及 Pine 脚本的搜索、读取源码、校验、`run_pine_indicator`（最多返回 1000 根）和 `run_pine_backtest` [TV-MCP]。文档没有提到画线工具。
- 条款 §27.3 只允许个人、非商业使用，禁止再分发，禁止系统性缓存、存储或抓取，禁止用于训练模型，禁止自动交易或通过 AI 客户端下单，禁止绕过限额 [TOU]；§27.4 只允许 AI 客户端为了回答本人提问、以人类可读形式展示而处理数据 [TOU]。

**推断**：MCP Server 适合“人在对话里问一下 TradingView 怎么看”，不适合做 Talaria 的数据源或指标源。

---

## 3. 类别 b：泄露或再分发的 `charting_library`

**只记录是否存在和官方表态；没有打开、下载或运行任何此类代码。**

### 3.1 TradingView 官方表态（事实）

- Get started 页：库托管在私有 GitHub 仓库，需要在产品页提交申请；库“is not redistributable”，禁止在公开仓库使用库的任何部分；从第三方服务获取库“is strictly prohibited” [TV-QS]。Update Library 页的 Redistribution 一节重复了“不可再分发、禁止进入公开仓库” [TV-UPD]。
- 免费图表库页：Advanced Charts 和 Trading Platform 不提供给“personal use, hobbies, studies, or testing”，许可只发给“companies for use in public web projects and/or applications”；个人项目请用 Apache-2.0 的 Lightweight Charts [TV-FREE]。
- FAQ：许可协议只允许把库用在公开网站上；Advanced Charts 和 Trading Platform 都不支持 Pine Script，自定义指标需要用 JavaScript 编写 [TV-FAQ]。
- 安装文档：npm 依赖写的是 `git@github.com:tradingview/charting_library.git`，只有已获授权的 GitHub 账号才能安装 [TV-NPM]。

### 3.2 GitHub 上的删除通知（事实）

`github/dmca` 仓库中，以 TradingView 或 ChartingLibrary 命名的通知共 7 份，时间从 2016-06-13 到 2019-04-15 [DMCA-2016][DMCA-2018-08][DMCA-2019]。其中：

- 2016-06-13 的通知由 TradingView, Inc. 发出，要求删除 4 个仓库里的 `charting_library` / `tv` 目录 [DMCA-2016]。
- 2018-08-13 的通知列出 5 个仓库，并写明许可协议不允许 fork 或分享库文件；还说这些用户不在其协作者或客户名单中，“means the library was stolen” [DMCA-2018-08]。
- 按年目录列出 2020–2026 年的文件名，没有找到以 TradingView 命名的通知。之后是否改用其他名义提交，**查不到**。

### 3.3 npm 上的疑似再分发（事实：仅 registry 元数据）

npm 搜索 `charting_library` 时，结果中有几个包的描述显示可能包含库本体：例如 `@suntree/charting_library`（描述为“CL v26.003 (internal id …)”）、`vax-tradingview`（描述称“This repository contains Charting Library package”）、`charting-library-downloader`（描述称用于下载 TradingView charting library 及其资源）[NPM-SEARCH]。**没有下载这些包核实内容。**

**推断**：任何来路不明的 `charting_library` 副本对 Talaria 都没有合法用法。官方明确不向个人授权，即使是公司许可也只限公开网站使用；Talaria 是本机单用户应用，不符合授权对象，也不符合使用场景。

---

## 4. 类别 c：Pine Script 解析器、转译器与运行时

PineTS（AGPL-3.0 / 商业许可）、PyneCore（Apache-2.0，`.pine` 转换器 PyneComp 不开源）和 pynescript（LGPL-3.0，仅解析器）已在 [chart-rule-scripting-sources.md](./chart-rule-scripting-sources.md) §3 核实，这里不再重复。下表只列该文“未核查”的项目（事实，读取于 2026-11-21）：

| 项目 | star | 最近推送 | 许可证 | 做什么 | 完整度（项目自述） |
|---|---|---|---|---|---|
| **`nullarch/resin`** | 约 14 | 2026-11-18 | Apache-2.0 | 把 Pine v5/v6 编译成纯 JavaScript 模块并运行 [RESIN] | 在 2,979 个真实脚本上，99.7% 能转译、99.5% 能运行（只说明能跑，不说明数值正确）；95 个场景与 TradingView 导出结果一致；但生成这些数字的测试工具“Not published yet” [RESIN] |
| **`Opus-Aether-AI/pine-transpiler`**（npm 0.4.11） | 约 40 | 2026-10-12 | MIT | 把 Pine v5/v6 转成 TradingView Charting Library 的 PineJS `CustomIndicator`；README 写明使用者必须持有 Charting Library 许可 [OPT] | `request.security` 只支持子集；有 LIMITATIONS 文档 [OPT] |
| **`pineforge-4pass/pineforge-engine`** + `pineforge-codegen-oss` | 约 215 / 约 10 | 2026-11-21 | 引擎 Apache-2.0；编译器为 PineForge Source License（源码可见，个人交易免费）[PF][PF-CG] | C++17 回测与执行引擎；Pine v6 → C++ 代码生成 [PF] | 自述在数千个评分探针上对齐 TradingView（数字由 README 模板生成，未核实）[PF] |
| **`pine2pyne/pine2pyne`**（PyPI 1.0.1，2026-11-20） | 0 | 2026-11-20 | Apache-2.0 | Pine v4/v5/v6 → PyneCore Python，本机离线运行 [P2P] | 仓库创建于 2026-11-20，过新，无法判断 |
| `LuxAlgo/pinets-cli` | 约 80 | 2026-10-13 | AGPL-3.0 | PineTS 的命令行 | 同 PineTS |
| `MeridianAlgo/Pine-A-Script`、`tradesdontlie/pinescript-compiler`、`loponly/pinescript-to-python` | 均不足 15 | — | — | 搜索可见，**未核查** | — |

**事实**：这些项目都不需要连接 TradingView，属于对公开语言文档的独立实现；README 均声明与 TradingView 无隶属关系，且 Pine Script 是 TradingView 的商标 [RESIN][OPT][PF]。

**推断**：

- 它们的运行时都基于 JS 的 `number` 或 C++/Python 的浮点；“与 TradingView 对齐”本身就是以浮点结果为基准。这与 Talaria 的 `Decimal` 黄金向量不是同一口径，结论与 chart-rule-scripting-sources.md §1 第 3 条一致。
- resin 和 PineForge 的对齐验证都依赖 TradingView 导出的结果做基准。Talaria 如果照这条路做，就会碰到 ToU §12(c)“不得用 TradingView 内容建数据集”的问题。

---

## 5. 类别 d：仿 TradingView 界面或交互的开源前端

| 项目 | star | 最近推送 | 许可证 | 底层 | 要点 |
|---|---|---|---|---|---|
| **`aztharion/klinechart-ui-core`** | 约 25 | 2026-11-11 | Apache-2.0 | KLineChart `^10.1.0` + React 19 + Tailwind v4 + Radix | 完整工作台组件：画线、多图布局，以及十字线、缩放、品种、周期同步（`WorkspaceProvider` / `useChartSync`）；浏览器可直连交易所 WebSocket [KUC]。仓库创建于 2026-11-10，很新 |
| **`NemeZZiZZ/react-klinecharts-ui`** | 约 20 | 2026-10-17 | GitHub 显示 NOASSERTION；LICENSE 文件为 Apache-2.0 | KLineChart，headless React | Provider + hooks + overlay 模板；11 个“TradingView-style”指标模板和画线是从 `dsavenk0/KLineChart-Pro`（QUANTIX Extended Edition）移植的 [RKU] |
| **`klinecharts/pro`**（npm `@klinecharts/pro` 0.1.1，2023-03-23） | 约 350 | 2026-07-26 | Apache-2.0 | KLineChart `>=9.0.0` + SolidJS | 官方开箱即用工具栏；npm 最新版仍是 2023 年的 0.1.1 [KPRO]。不建议直接引入的理由见 copilot-trading-workbench-sources.md §3.2 |
| **`dylanpersonguy/OpenCharts`** | 约 310 | 2026-11-10 | MIT | Lightweight Charts `^4.2.0` + 自写插件 | 浏览器内交易终端：画线工具栏、对象树、8 个指标、回放、模拟交易；经济日历等标签页直接嵌入 TradingView 官方 widget [OC] |
| **`deepentropy/lightweight-charts-drawing`**（npm 0.11.1，2026-11-19） | 约 160 | 2026-11-19 | MIT | Lightweight Charts v5 插件 | 86 个画线工具，自述采用“TradingView's factory defaults”、点击和拖拽规则 [LCD] |
| **`difurious/lightweight-charts-line-tools-core`** | 约 110 | 2026-07-16 | MPL-2.0 | Lightweight Charts v5 插件 | 画线编排核心：坐标插值、无限线裁剪、选中状态、锚点拖拽、坐标轴标签堆叠；前身是修改过源码的 v3.8 分支（Apache-2.0，已弃用）[LTC][LT-OLD] |

**事实**：这些项目都基于 Apache-2.0 的 KLineChart 或 Lightweight Charts 自行实现，不含 TradingView 专有代码（以 README 和依赖声明为准，未审计源码）。

**推断**：

- 与 Talaria 同栈（KLineChart v10.1.1，见 `dsh/packages/talaria-desk-ui/package.json`）、最值得读的是 `klinechart-ui-core` 的多图同步和工具栏结构，以及 `react-klinecharts-ui` 的 headless hooks 划分。两者都是 Apache-2.0，可以借鉴代码，但需保留版权与 NOTICE。
- `react-klinecharts-ui` 的部分代码移植自第三方分叉，引入前要核对上游 `dsavenk0/KLineChart-Pro` 的许可（本文**未核查**）。
- `lightweight-charts-line-tools-core` 是 MPL-2.0（文件级 copyleft）：直接引入可以，修改它的文件就必须以 MPL 开源；如果只看交互算法，则没有许可问题。

---

## 6. 法律与条款风险

### 6.1 TradingView Terms of Use（事实，[TOU]，页面无“最后更新”日期）

| 条款 | 要点（短引原文，其余为转述） |
|---|---|
| **§3** 标题含“redistribution of data; non-display usage” | 图表和市场数据的许可仅限个人或内部业务用途，且只能用于显示；“explicitly prohibits any form of non-display usage”；也禁止使用帮助实现非显示用途的第三方工具 |
| **§4** 署名 | 在 TradingView 网站以外使用其产品（包括 widget）必须署名 TradingView，违者可能被永久封禁 |
| **§12(a)** 自动化访问 | 禁止用 robot、spider、scraper、crawler 等自动化手段收集数据，包括访问“internal or undocumented endpoints or real-time data feeds”和绕过限流；例外仅限按 robots.txt 建索引的搜索引擎，以及官方明确提供的 API 或接口 |
| **§12(b)** 逆向 | 禁止“reverse engineer, decompile, disassemble, decrypt”，或推导源码、算法、非公开接口，除非适用法律明确允许 |
| **§12(c)** 数据集与训练 | 禁止用 TradingView 内容（含图表、脚本、AI 输出）训练或测试模型，或构建数据库、数据集 |
| **§12(d)** 竞品 | 禁止用 TradingView 构建与其竞争的产品，“including by replicating its features, functions, or user interface” |
| **§12(e)** 工具 | 禁止开发或分发实现 (a)–(d) 的软件、浏览器扩展、脚本、工具或服务 |
| **§18** 终止 | 可不经通知立即终止、限制或暂停账号；违反 §12 会自动终止许可 |
| **§27** MCP Server | 见本文 §2.4；§27.8 规定可暂停或撤销 MCP 访问权限 |

在 [chart-rule-scripting-sources.md](./chart-rule-scripting-sources.md) §5 读取时（2026-11-20），记录的是 §12(b)(d)，没有记录 (a)(c)(e)。本次读到的 §12(a)–(e) 和 §27 是否为近期新增，因为页面没有日期，**无法判断**。

### 6.2 Advanced Charts 许可（事实）

见本文 §3.1：库不可再分发，禁止从第三方获取，不向个人授权，只能用于公开网站 [TV-QS][TV-UPD][TV-FREE][TV-FAQ]。许可协议全文不公开，**查不到**。

### 6.3 交易所行情再分发（事实）

- Nasdaq《US Equities and Options Data Policies》v2.6（© 2022，最近更新记录为 2024-06-01）开篇写明：Distributor 依据 Nasdaq Global Data Agreement 必须遵守这些政策 [NQ-POL]。
- 第 2 节对“Information”的定义，包括能从处理后结果中识别、重新计算或反推出交易所信息的数据；第 7 节把 Non-Display 定义为由机器自动访问、没有自然人查看显示屏的使用方式，计费单位包括运行计算或生成衍生数据的服务器 [NQ-POL]。
- TradingView 在 ToU §10 自称是向终端用户提供交易所官方实时行情的 vendor [TOU]。也就是说，用户从 TradingView 拿到的美股行情，同时受 TradingView 条款（仅显示、禁止再分发）和交易所数据政策约束。
- 加密货币交易所行情的再分发条款**本文未调研**。

**推断**：把非官方接口拿到的美股行情存进 Talaria 冷存储，再交给策略或预警计算，就构成 TradingView 条款禁止的非显示使用，也落在 Nasdaq 对 Non-Display 的定义里。对单用户本机项目，交易所实际追责的可能性低，但风险主要来自 TradingView 封号和接口失效；而这两点都会直接破坏 Talaria“数据可复现”的前提。

---

## 7. 对 Talaria 的结论

### 7.1 可以用（推断）

| 做法 | 依据 |
|---|---|
| 参照 TradingView **公开文档**中的交互概念（布局、画线同步模式、多图同步），用 KLineChart 自己实现 | 只读文档、不使用服务，不触发 §12(a)(b)；产品形态调研已在用这种方式 [local-multi-frontend-product-form.md] |
| 借鉴或引入 Apache-2.0 / MIT 的开源仿制前端代码：`klinechart-ui-core`、`react-klinecharts-ui`、`lightweight-charts-drawing`；MPL-2.0 的 `line-tools-core` 按文件级 copyleft 处理 | 许可允许；与 TradingView 代码无关 [KUC][RKU][LCD][LTC] |
| 用户本人在 Claude 对话中通过官方 MCP Server 查看 TradingView 的看法，作为人工参考 | §27.3–27.4 允许个人、仅供显示的用途 [TOU][TV-MCP] |
| 自写类 Pine 规则语言（或表达式树），不使用 TradingView 名称，也不以 TradingView 输出作为基准 | 见 chart-rule-scripting-sources.md §5–§6 |

### 7.2 不能用或高风险（推断）

| 做法 | 问题 |
|---|---|
| 任何来源的泄露 `charting_library` | 违反再分发禁令，且官方不向个人授权；有 DMCA 先例 [TV-QS][DMCA-2018-08] |
| 用 `TradingView-API`、`tvdatafeed` 等作为 K 线或指标数据源 | 违反 §12(a)(e)；有封号、验证码、接口失效的记录 [MC-I179][TVDF-I62][TVDF-I63] |
| 把 TradingView 算出的指标值（含 MCP `run_pine_indicator`）存下来，或交给策略或预警 | 违反 §3 非显示条款和 §27.3 存储禁令；与 ADR-0010 冲突（见 7.3） |
| 用 TradingView 输出做 Talaria 指标的“对拍”基准，并保存成测试数据 | §12(c) 禁止建数据集；同时会让黄金向量依赖外部不可复现的值 |
| 通过 CDP 驱动桌面版（`tradesdontlie/tradingview-mcp` 的方式）喂数据给 Talaria | 该项目自己承认可能与 ToU 冲突，且依赖未公开的内部结构 [TDL-README] |
| 对着 TradingView 页面逐项复制功能清单和界面 | §12(d) 明文禁止“replicating its features, functions, or user interface”；Talaria 自用、不对外，实际风险低，但不建议把“复刻 TradingView”作为需求表述 |

### 7.3 与 ADR-0010 的冲突（事实 + 推断）

- **事实**：ADR-0010 规定 `core/src/talaria/foundation/operators/` 的纯 Python 实现是唯一数值来源，`get_indicators`、预警引擎和浏览器副图都使用它；明确否决前端或 JS 库计算、Python 只做转发；新的适配器必须先通过 `contracts/operator_vectors/` 黄金向量 [ADR-0010]。
- **推断**：TradingView 服务端指标在三方面无法满足这些要求：
  1. 算法不公开，`Decimal` 精度无法确认，无法通过黄金向量；
  2. 条款禁止存储和非显示使用，所以图表、策略和预警不能共用同一份值；
  3. 可用性取决于账号状态和协议版本，回测无法复现。
  即使只在图表上叠加 TradingView 的值作“参考线”，也会出现两套数值并存，正是 ADR-0010 要避免的情况。

### 7.4 值得借鉴的开源项目清单（推断，按与 Talaria 的相关度排序）

1. `aztharion/klinechart-ui-core`：同为 KLineChart v10 + React，参考工具栏、多图同步和工作区结构 [KUC]。
2. `NemeZZiZZ/react-klinecharts-ui`：参考 headless hooks 与 Datafeed 抽象；移植的上游代码许可需另行核对 [RKU]。
3. `difurious/lightweight-charts-line-tools-core`：参考画线交互的难点（无限线裁剪、锚点拖拽、坐标轴标签堆叠）[LTC]。
4. `deepentropy/lightweight-charts-drawing`：参考 86 种画线工具的清单和命中测试 [LCD]。
5. `dylanpersonguy/OpenCharts`：参考终端整体布局、对象树和回放 [OC]。

---

## 8. 查不到或存疑

- `StreamAlpha/tvdatafeed` 原仓库现返回 404，PyPI 上也查不到 `tvdatafeed` 包；原项目何时、为何下线，查不到一手说明。
- `TradingView-API` 的 issue #179（封号）没有 TradingView 方面的说明；TradingView 是否有专门检测非官方客户端的公开政策，查不到。
- TradingView 帮助中心是否有“无公开数据 API”的正式说明：本次只找到二手文章转述，没有读到一手页面，因此不采信。
- Advanced Charts 许可协议全文不公开；“仅限公开网站”只见于 FAQ 的一句话 [TV-FAQ]。
- `github/dmca` 中 2020 年以后是否还有针对 `charting_library` 的通知：按文件名查不到，但通知可能以代理人或其他名义提交。
- npm 上疑似再分发的包是否真的包含库代码：没有下载，无法确认 [NPM-SEARCH]。
- ToU §12(a)(c)(e) 和 §27 的生效日期：页面没有日期。
- resin、PineForge 的对齐数字，以及 `Opus-Aether-AI/pine-transpiler` 的测试覆盖率，都是项目自述；resin 自己也说明测试工具尚未公开 [RESIN]。
- `react-klinecharts-ui` 上游 `dsavenk0/KLineChart-Pro` 的许可证：未核查。
- 加密货币交易所行情的再分发条款：未调研。

---

## 9. 来源

### TradingView 官方（读取于 2026-11-21）

- [TOU] Terms of Use, Policies and Disclaimers（§3、§4、§10、§12(a)–(e)、§18、§27）：https://www.tradingview.com/policies/
- [TV-MCP] TradingView MCP Server Beta 文档：https://www.tradingview.com/mcp/docs
- [TV-QS] Advanced Charts 文档，Get started：https://www.tradingview.com/charting-library-docs/latest/quick-start/
- [TV-UPD] Advanced Charts 文档，Update Library（Redistribution 一节）：https://www.tradingview.com/charting-library-docs/latest/releases/Update-Library
- [TV-NPM] Advanced Charts 文档，NPM：https://www.tradingview.com/charting-library-docs/latest/getting_started/NPM/
- [TV-FAQ] Advanced Charts 文档，Frequently Asked Questions：https://www.tradingview.com/charting-library-docs/latest/getting_started/Frequently-Asked-Questions/
- [TV-FREE] TradingView 免费图表库页面：https://www.tradingview.com/free-charting-libraries/

### GitHub DMCA

- [DMCA-2016] https://github.com/github/dmca/blob/master/2016/2016-06-13-TradingView.md
- [DMCA-2018-08] https://github.com/github/dmca/blob/master/2018/2018-08-13-ChartingLibrary.md
- [DMCA-2019] https://github.com/github/dmca/blob/master/2019/04/2019-04-15-tradingview.md
- 其余 4 份同类通知：`2017/2017-10-04-TradingView.md`、`2018/2018-02-01-TradingView.md`、`2018/2018-03-15-TradingView.md`、`2018/2018-08-16-TradingView.md`，均位于 https://github.com/github/dmca

### 交易所

- [NQ-POL] Nasdaq，US Equities and Options Data Policies v2.6（§2 Terminology，§7 Non-Display Usage Policy，§11 Document Updates）：https://www.nasdaqtrader.com/content/AdministrationSupport/Policy/USEquitiesandOptionsDataPolicies.pdf

### 类别 a 项目

- [MC-README] Mathieu2301/TradingView-API README：https://github.com/Mathieu2301/TradingView-API
- [MC-PKG] 同仓库 `package.json`（`"license": "ISC"`，4.1.2）：https://github.com/Mathieu2301/TradingView-API/blob/main/package.json
- [MC-CLIENT] 同仓库 `src/client/client.ts` L138–L139（WebSocket URL 与 Origin）：https://github.com/Mathieu2301/TradingView-API/blob/main/src/client/client.ts
- [MC-COV] 同仓库 `docs/v4-coverage.md`：https://github.com/Mathieu2301/TradingView-API/blob/main/docs/v4-coverage.md
- [MC-GS] 同仓库 `docs/getting-started.md`（Accounts and limits）：https://github.com/Mathieu2301/TradingView-API/blob/main/docs/getting-started.md
- [MC-I179] issue #179“Calculation timed out” error and account bans：https://github.com/Mathieu2301/TradingView-API/issues/179
- [TVDF-README] rongardF/tvdatafeed README：https://github.com/rongardF/tvdatafeed
- [TVDF-SRC] 同仓库 `tvDatafeed/main.py` L33–L37、L87：https://github.com/rongardF/tvdatafeed/blob/main/tvDatafeed/main.py
- [TVDF-I62] issue #62（recaptcha_required）：https://github.com/rongardF/tvdatafeed/issues/62
- [TVDF-I63] issue #63（get_hist() stopped working）：https://github.com/rongardF/tvdatafeed/issues/63
- [TA-README] AnalyzerREST/python-tradingview-ta README（已归档）：https://github.com/AnalyzerREST/python-tradingview-ta
- [TA-SRC] 同仓库 `tradingview_ta/main.py` L49（`scan_url`）：https://github.com/AnalyzerREST/python-tradingview-ta/blob/main/tradingview_ta/main.py
- [TVS-README] shner-elmo/TradingView-Screener README：https://github.com/shner-elmo/TradingView-Screener
- [MNW-README] mnwato/tradingview-scraper README：https://github.com/mnwato/tradingview-scraper
- [TVSC-README] smitkunpara/tv-scraper README：https://github.com/smitkunpara/tv-scraper
- [TVRS-README] vnquants/tradingview-rs README：https://github.com/vnquants/tradingview-rs
- [TDL-README] tradesdontlie/tradingview-mcp README（含 Disclaimer）：https://github.com/tradesdontlie/tradingview-mcp
- [ATA-README] atilaahmettaner/tradingview-mcp README：https://github.com/atilaahmettaner/tradingview-mcp
- [JCH-README] jchao01/TradingView-data-scraper README：https://github.com/jchao01/TradingView-data-scraper
- [IMX-README] imxeno/tradingview-scraper README：https://github.com/imxeno/tradingview-scraper
- PyPI 元数据：https://pypi.org/project/tradingview-ta/ 、https://pypi.org/project/tradingview-screener/ 、https://pypi.org/project/tradingview-scraper/ 、https://pypi.org/project/tv-scraper/ 、https://pypi.org/project/tradingview-rs/
- npm 元数据：https://www.npmjs.com/package/@mathieuc/tradingview

### 类别 b

- [NPM-SEARCH] npm registry 搜索 `charting_library`（只读元数据）：https://registry.npmjs.org/-/v1/search?text=charting_library

### 类别 c 项目

- [RESIN] nullarch/resin README：https://github.com/nullarch/resin
- [OPT] Opus-Aether-AI/pine-transpiler README：https://github.com/Opus-Aether-AI/pine-transpiler
- [PF] pineforge-4pass/pineforge-engine README：https://github.com/pineforge-4pass/pineforge-engine
- [PF-CG] pineforge-4pass/pineforge-codegen-oss README 与 LICENSE：https://github.com/pineforge-4pass/pineforge-codegen-oss
- [P2P] pine2pyne/pine2pyne README：https://github.com/pine2pyne/pine2pyne
- LuxAlgo/pinets-cli：https://github.com/LuxAlgo/pinets-cli

### 类别 d 项目

- [KUC] aztharion/klinechart-ui-core README 与 `package.json`：https://github.com/aztharion/klinechart-ui-core
- [RKU] NemeZZiZZ/react-klinecharts-ui README 与 LICENSE：https://github.com/NemeZZiZZ/react-klinecharts-ui
- [KPRO] klinecharts/pro `package.json`：https://github.com/klinecharts/pro/blob/main/package.json ；npm：https://www.npmjs.com/package/@klinecharts/pro
- [OC] dylanpersonguy/OpenCharts README：https://github.com/dylanpersonguy/OpenCharts
- [LCD] deepentropy/lightweight-charts-drawing README：https://github.com/deepentropy/lightweight-charts-drawing
- [LTC] difurious/lightweight-charts-line-tools-core README：https://github.com/difurious/lightweight-charts-line-tools-core
- [LT-OLD] difurious/lightweight-charts-line-tools（已弃用）：https://github.com/difurious/lightweight-charts-line-tools

### 仓库内

- [ADR-0010] `docs/adr/0010-pure-python-indicator-core.md`
