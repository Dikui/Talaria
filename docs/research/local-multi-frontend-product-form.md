# 本机常驻数据服务 + 多个可单独打开的前端：产品形态一手资料调研

- **调研日期**：2026-11-20（Asia/Shanghai）。
- **范围**：对应 `.scratch/platform-redesign/spec.md`“需要调研的空白”第 2 项：本机常驻数据服务 + 多个可单独打开的前端。具体包括 TradingView 桌面版/Web 版的多窗口与多图表做法、Advanced Charts 的 Datafeed 解耦方式；浏览器多页面、Tauri v2、Electron、PWA 四种前端承载形态；macOS launchd 守护与由桌面应用托管后台进程的对比；两个开源交易软件的“本机服务 + 浏览器前端”做法。
- **方法**：只读官方帮助中心、官方博客、官方发布说明、官方开发文档、MDN 与 `mdn/browser-compat-data`、Apple 开发者文档与本机 `man launchd.plist`（macOS 27.2.1），以及官方源码（标注提交）。**未安装、未运行任何外部软件，未登录。**
- **标注约定**：**事实**＝来源原文可直接核对；**厂商宣称**＝厂商对自家产品的性能/体积等表述，未独立验证；**推断**＝面向 Talaria 的分析和建议，不是来源结论。
- **与已有调研的关系**：图表库选型（KLineChart v10 vs Lightweight Charts）见 [copilot-trading-workbench-sources.md](./copilot-trading-workbench-sources.md)；dsh Web 插槽与 Profile 机制见 [deepseek-harness-runtime-survey.md](./deepseek-harness-runtime-survey.md)；Freqtrade/FreqUI 的 GPL-3.0 许可边界见前者 §3。本文不重复这些结论。

---

## 1. 核心结论

1. **TradingView 的“单独打开 / 一起用”有两层：窗口层（桌面版多窗口、标签页、标签组、分离 Pine 编辑器）和图表层（一个布局内最多 8/16 个图表、按品种/周期/十字线/时间同步）。数据与图表在其嵌入式库里是硬解耦的。** 桌面版官方发布说明逐版写明升级了 Electron 版本（3.5.1 → Electron 45）[TV-RN]；Advanced Charts 文档写明库本身“不含任何行情数据”，由集成方实现 `onReady / searchSymbols / resolveSymbol / getBars / subscribeBars / unsubscribeBars` 六个方法供图表调用 [TV-DF][TV-DF-REQ]。
2. **浏览器多页面形态零新增依赖，同源页面间通信在现行浏览器均可用。** `BroadcastChannel` 自 2022-03 起为 Baseline 广泛可用；`SharedWorker` 在 Safari 16 恢复支持、Chrome Android 148 才加入，MDN 标为“Baseline 2026 新近可用” [MDN-BC][MDN-SW][BCD]。但浏览器形态没有任何机制管理后台 Python 进程的生命周期。
3. **Tauri v2 和 Electron 都能做多窗口 + 托管 Python 子进程，但“随应用退出而终止”只覆盖正常退出路径。** Tauri shell 插件在 `RunEvent::Exit` 时只杀掉经 JS 端 `spawn` 登记的子进程 [TAURI-SHELL-SRC]；Electron `utilityProcess` 文档未说明随应用退出的行为（查不到）。两者在 macOS 上对外分发都要求 Developer ID 签名 + 公证 [TAURI-SIGN][EL-SIGN]。
4. **launchd 是 macOS 上唯一由系统负责“开机/登录常驻 + 崩溃重启”的机制，且可由 App 通过 `SMAppService.agent(plistName:)`（macOS 13+）注册，Electron 也已暴露该能力。** 但 `KeepAlive` 有默认 10 秒的重启节流，未指定 `ProcessType` 时系统会限制 CPU 与 I/O [LAUNCHD-MAN][SMAPP]。
5. **开源交易软件（Freqtrade、Jesse）都采用“本机 Python 服务自带 HTTP 服务 + 浏览器打开仪表盘”的形态**，与 Talaria 现有 `talaria up` 一致 [FT-API][JESSE]。未找到有官方源码可核实的开源看盘软件做“原生多窗口桌面壳 + 独立数据守护进程”。

---

## 2. TradingView：多窗口、多图表与数据解耦

### 2.1 桌面版与 Web 版的窗口/标签做法（事实）

| 能力 | 官方表述 | 来源 |
|---|---|---|
| 多窗口 | 桌面版“可以按需打开任意多个窗口”，主要用于多显示器，每块屏一个窗口；新窗口从标题栏按钮或应用菜单打开 | [TV-MW] |
| 分离标签页成窗口 | 2022-04-27 博客：免费用户可用多显示器，取消窗口数上限；`Ctrl/Cmd+N` 或把标签页拖出成新窗口；需桌面版 1.0.6+ | [TV-BLOG-MM] |
| 窗口状态恢复 | 下次启动恢复所有窗口及位置；需用“退出”而非逐个关窗 | [TV-MW] |
| 跨窗口同步 | 2022-03-24 博客：十字线可跨“多个标签页和窗口”同步，按布局开关；可与 Tab Linking 合用 | [TV-BLOG-XH] |
| 标签组 | 桌面版 3.5（2026-10-29）加入；最多 10 组；切换组时离开的组的标签页关闭、不在后台运行；一个组同一时间只能在一个窗口打开；每个窗口保留自己的未分组标签页；组只存本机、不跨设备同步 | [TV-BLOG-TG][TV-RN] |
| 分离的 Pine 编辑器 | 从图表打开的分离标签页/窗口“与该图表保持链接”，可从分离页向图表添加和更新脚本；状态栏显示链接状态 | [TV-PINE] |
| 布局 / 图表定义 | 布局包含图表、设置、画线、指标，由唯一 URL 标识；自选和预警**不**存于布局；一个布局 1–16 个图表，上限取决于订阅等级 | [TV-LAYOUT] |
| 画线同步 | 三种模式：仅本图表 / 按布局（同品种的所有图表）/ 全局（所有图表和布局）；跨窗口、跨设备实时同步需开启自动保存 | [TV-LAYOUT] |
| 同一布局开在多个标签页 | 官方提示一个标签页的修改可能与其他标签页冲突 | [TV-LAYOUT] |

### 2.2 桌面版技术栈（事实 + 未查到部分）

- **事实**：官方发布说明逐版记录 Electron 升级：1.0.8（2022-05-25）“Update to Electron 18”、2.0.0（2022-12-21）Electron 21.3.0、2.14.0（2025-10-20）Electron 38、3.4.0（2026-08-27）Electron 41、3.5.1（2026-11-17）Electron 45；3.5.1 起 macOS 最低版本为 Ventura（13）[TV-RN]。
- **事实**：发布说明还记录 2.7.6 起行情数据和定价页改在系统浏览器打开、3.5.0 起券商登录改在浏览器完成 [TV-RN]。
- **查不到一手资料**：桌面版的进程结构（是否每窗口一个渲染进程、图表代码是否从网站远程加载）、数据连接是否跨窗口共享、离线能力。社区逆向分析不属一手来源，未采信。

### 2.3 Advanced Charts / Trading Platform 的 Datafeed 与多图表（事实）

- 库“不含任何行情数据”，数据由集成方提供；接入方式有两种：自行实现 JS Datafeed API，或用内置 UDF 适配器 [TV-DF]。
- 六个必需方法：`onReady`（配置：交易所、品种类型、周期）、`searchSymbols`、`resolveSymbol`（品种元数据：交易所、时区、交易时段）、`getBars`（历史区间 K 线）、`subscribeBars`、`unsubscribeBars` [TV-DF-REQ]。
- 实时约束：`subscribeBars` 回调只能更新最新一根或追加新 K 线，**不能改历史 K 线**；库会延迟约 5 秒才调用 `unsubscribeBars`；以 `listenerGuid` 区分订阅 [TV-DF-REQ]。库可能修改传入的 bar 数据，应传副本 [TV-DF]。
- 多图表布局是 **Trading Platform**（非 Advanced Charts）功能：一个布局最多 8 个图表，可按品种、周期、十字线、时间、日期范围同步；API 有 `setLayout()`、`layout()`、`chart(index)`、`activeChart()`、`chartsCount()`、`setLayoutSizes()`、`unloadUnusedCharts()` [TV-MCL]。
- **查不到**：文档未说明多图表是否共用同一 Datafeed 实例（构造时只传一个 `datafeed`，按常理是共用，属推断）。

**推断（对 Talaria）**：Datafeed 六方法接口是“图表只消费、不持有数据”的成熟参照。Talaria 若把数据服务定义为唯一数据源，前端图表适配层可按“配置 / 品种解析 / 历史区间 / 实时订阅 / 退订”五类请求设计，与 KLineChart v10 的 `setDataLoader` 对接；“实时推送不改历史”的约束与冷热库口径（ADR-0011）一致。

---

## 3. 前端承载形态对比

### 3.1 浏览器多标签页 / 多窗口（事实）

- `BroadcastChannel`：同源的窗口、标签页、iframe、worker 之间按频道名广播，发送者自己不收；MDN 标 Baseline“广泛可用”，自 2022-03 起各浏览器可用 [MDN-BC]。BCD 版本：Chrome 54、Firefox 38、Safari 15.4 [BCD]。
- `SharedWorker`：多个同源（协议、主机、端口完全相同）上下文共享一个 worker；只要还有页面引用就存活 [MDN-SW]。MDN 标“Baseline 2026 新近可用”（2026-05 起）[MDN-SW]。BCD：Chrome 5、Firefox 29、Safari 16（5–7 曾支持后移除）、Chrome Android 148 [BCD]。
- **推断**：Talaria 已由 FastAPI 在 `127.0.0.1` 同源托管 SPA（`core/src/talaria/workbench_api/routes/static.py` 的 `/desk/{path}`），把图表、快讯、预警做成同一 SPA 的不同路由即可各自在新标签页/窗口打开，前端代码几乎全量复用。跨页联动（切换品种、十字线同步）可用 `BroadcastChannel`；如果想让多个页面共用一条到后端的 WebSocket，可用 `SharedWorker`，但 Safari 16 以下不可用。浏览器不管理后台进程，后端仍需 `talaria up` 或 launchd 启动。
- **注意（推断）**：现有鉴权 token 以 `SameSite=strict` cookie 下发，`127.0.0.1` 与 `localhost` 是不同源，多页面须统一用一个主机名。

### 3.2 PWA 安装为独立窗口（事实）

- 可安装条件：manifest（Chromium 要求 `name`/`short_name`、192 与 512 图标、`start_url`、`display`）；必须 `https` 或 `localhost` / `127.0.0.1`（可带端口）；service worker 不是必需 [MDN-PWA]。
- 桌面支持：Chrome、Edge 在所有桌面系统支持安装；Safari 17（macOS Sonoma）起支持“文件 > 添加到程序坞”，有无 manifest 均可；Firefox 不支持基于 manifest 安装 [MDN-PWA]。web.dev 的“Safari for macOS 不支持安装”一句与 MDN、WebKit 官方博客矛盾，以后两者为准 [WEBDEV-PWA][WEBKIT-17]。
- Safari 添加到程序坞时复制 cookie，**不复制其他本地存储**，之后与 Safari 不再共享网站数据 [WEBKIT-17]。
- 已安装 PWA 有独立窗口、出现在程序坞与应用切换器；Chromium 可在 `about:apps` 设置“登录时启动”[WEBDEV-PWA]。
- **推断**：PWA 只改变“窗口外观和启动入口”，不改变后台进程管理。它是在浏览器方案上加几行 manifest 的低成本增强；每个 feature 做成独立 PWA 需要不同 `start_url`/`scope`，同一浏览器同一应用不能重复安装 [WEBDEV-PWA]。

### 3.3 Tauri v2 多窗口 + sidecar（事实）

- **多窗口**：JS 端 `new WebviewWindow(label, { url })` 创建窗口，label 唯一；创建窗口需要 `core:webview:allow-create-webview-window` 权限，不在默认权限内 [TAURI-WVW]。能力（capabilities）按窗口 label 授权，不同窗口可授予不同插件权限 [TAURI-CAP]。
- **sidecar**：`tauri.conf.json` 的 `bundle.externalBin` 声明外部可执行文件，文件名需带 `-$TARGET_TRIPLE` 后缀；文档称常见用途是“用 pyinstaller 打包的 Python CLI 或 API 服务”；需在 capability 中授予 `shell:allow-execute` 或 `shell:allow-spawn`，参数可用正则校验 [TAURI-SIDECAR]。
- **生命周期（源码事实）**：`tauri-apps/plugins-workspace` 提交 `baf8b01`，`plugins/shell/src/lib.rs` 在 `RunEvent::Exit` 时遍历 `children` 并 `kill()`；该表只在 `commands.rs` 的 JS `spawn` 命令里登记 [TAURI-SHELL-SRC]。即：从 Rust 端 `shell().sidecar().spawn()` 启动的子进程不在此表，需自行管理；应用崩溃时不会走 `RunEvent::Exit`（后半句为推断）。
- **崩溃重启 / 开机常驻**：sidecar 文档未提供（查不到官方内置方案）。
- **体积（厂商宣称）**：“最小 Tauri 应用可小于 600KB”，因使用系统自带 webview 而非打包浏览器引擎 [TAURI-START]。macOS 上是 WKWebView，随系统更新，不受支持的 macOS 版本不再获得 WebKit 更新 [TAURI-WV]。**推断**：Talaria 的 Python 后端用 PyInstaller 打包后体积主要由 Python 运行时与依赖决定，600KB 数字对整体意义不大；官方未给内存数据（查不到）。
- **签名公证**：对外分发需 Apple Developer Program 付费账户与 “Developer ID Application” 证书并公证；可用 `signingIdentity: "-"` 做 ad-hoc 签名，但仍需用户在“隐私与安全性”手动放行 [TAURI-SIGN]。文档未说明 sidecar 是否需单独签名（查不到）。
- **推断（复用度）**：React/Vite 代码可原样作为 Tauri 前端；但 WKWebView 与现有开发用浏览器可能存在 WebKit 差异，需按 [TAURI-WV] 的版本表核对。若 Tauri 窗口直接加载 `http://127.0.0.1:<port>/desk`，现有 cookie 鉴权与同源托管不变；若用打包进应用的静态资源，则前端与后端跨源，需要 CORS 与新的 token 传递方式。

### 3.4 Electron 多 BrowserWindow + 子进程（事实）

- **多窗口**：主进程用 `BrowserWindow` 创建和管理窗口，每个 `BrowserWindow` 在独立渲染进程中加载页面，一个页面崩溃不影响其他页面 [EL-PM]。
- **子进程**：`utilityProcess` 用 Chromium Services API 启动带 Node.js 的子进程，等价于 `child_process.fork`；可与渲染进程建立 `MessagePort` [EL-PM][EL-UP]。`child.kill()` 在 POSIX 发 SIGTERM；`exit` 事件在子进程结束后必然触发 [EL-UP]。Python 后端仍需 Node 的 `child_process.spawn` 启动（`utilityProcess` 只跑 JS），此为推断。
- **生命周期**：未订阅 `window-all-closed` 时，所有窗口关闭即退出应用 [EL-APP]。`utilityProcess` 是否随应用退出被终止，文档未写（查不到）；崩溃重启无内置机制，需监听 `exit` 自行重启（推断）。
- **开机常驻**：`app.setLoginItemSettings` 在 macOS 支持 `type: 'agentService'`，plist 须位于 `Contents/Library/LaunchAgents`，即对接 Apple `SMAppService`；文档要求应用签名并公证，否则 `openAtLogin` 可能静默失效 [EL-APP]。
- **体积/资源**：官方性能文档只说明 Electron 自带 Chromium，**未给体积或内存数字**（查不到）[EL-PERF]。
- **签名公证**：需 Apple Developer Program、Xcode；Forge 内部用 `@electron/osx-sign` 和 `@electron/notarize`；未签名应用会出现“已损坏”提示；`safeStorage`、登录项、自动更新（Squirrel.Mac）依赖签名 [EL-SIGN]。
- **推断（复用度）**：React/Vite 代码可原样复用；渲染引擎是固定版本的 Chromium，与开发浏览器一致性好于 WKWebView；代价是应用自带 Chromium，且多一套 Node 主进程代码。

### 3.5 对比表（事实列标来源，空格处表示官方未提供）

| 维度 | 浏览器多页面 | PWA | Tauri v2 | Electron |
|---|---|---|---|---|
| 多窗口 | 浏览器标签/窗口；同源通信 `BroadcastChannel`/`SharedWorker` [MDN-BC][MDN-SW] | 独立应用窗口 [WEBDEV-PWA] | `WebviewWindow`，按窗口授权 [TAURI-WVW][TAURI-CAP] | 每窗口独立渲染进程 [EL-PM] |
| 随应用启停后台进程 | 无 | 无 | 正常退出时杀 JS 端 spawn 的子进程 [TAURI-SHELL-SRC] | 需自写；`utilityProcess.kill()` [EL-UP] |
| 开机常驻 | 无（交给 launchd） | Chromium“登录时启动”只启动窗口 [WEBDEV-PWA] | 官方 sidecar 文档未提供 | `setLoginItemSettings` + `agentService` [EL-APP] |
| 崩溃重启 | 无 | 无 | 未提供 | 需自写（推断） |
| 体积/资源官方数据 | 无新增 | 无新增 | 厂商宣称最小 <600KB [TAURI-START] | 查不到 |
| macOS 签名公证 | 不需要 | 不需要 | Developer ID + 公证 [TAURI-SIGN] | Developer ID + 公证 [EL-SIGN] |
| 现有 React/Vite 复用 | 全量 | 全量 + manifest | 高；WKWebView 差异需核对 [TAURI-WV] | 高 |

---

## 4. 本机常驻服务的进程管理：launchd vs 桌面应用托管

### 4.1 launchd（事实，来源：本机 `man launchd.plist` 与 Apple 文档）

- **代理与守护进程**：用户级 Agent 只在该用户登录时运行，plist 放 `~/Library/LaunchAgents`；系统级 Daemon 放 `/Library/LaunchDaemons`，归 root [APPLE-LAUNCHD]。单用户本机场景对应 Agent。
- **常驻与重启**：`KeepAlive` 默认 false（仅按需启动）；设为 true 则无条件保活，且隐含 `RunAtLoad`；可用字典条件，如 `SuccessfulExit=false` 表示非零退出时重启；快速反复退出的任务会被节流 [LAUNCHD-MAN]。
- **节流**：`ThrottleInterval` 默认同一任务 10 秒内最多启动一次 [LAUNCHD-MAN]；Apple 指南提醒按需启动的服务启动后至少 10 秒内不要退出，否则可能被视为崩溃 [APPLE-LAUNCHD]。
- **资源限制**：未设 `ProcessType` 时系统施加轻度限制，节流 CPU 与 I/O；`Standard` 等同未设；`Interactive` 与前台应用同等（无限制），仅在应用响应依赖它时使用；`Adaptive` 随 XPC 活动在 Background/Interactive 间切换 [LAUNCHD-MAN]。
- **禁止自行守护化**：不得调用 `daemon()` 或 fork 后退出，否则 launchd 认为进程已死 [APPLE-LAUNCHD][LAUNCHD-MAN]；应处理 SIGTERM 并及时退出，`ExitTimeOut` 控制 SIGTERM 到 SIGKILL 的间隔 [LAUNCHD-MAN]。
- **进程组**：任务退出时 launchd 会杀掉同进程组的残留进程（除非 `AbandonProcessGroup`）[LAUNCHD-MAN]。
- **按需（socket）启动**：`Sockets` / `MachServices` 让 launchd 在客户端连接时才拉起任务；launchd 没有显式依赖模型，依赖靠 IPC 解决 [LAUNCHD-MAN]。
- **不建议**：`RunAtLoad` 应避免（影响开机/登录）；`WatchPaths` 等因竞态不建议 [LAUNCHD-MAN]。
- **隐私**：launchd 管理的任务受 macOS 用户隐私保护约束，plist 指向隐私敏感目录可能导致任务无法运行 [LAUNCHD-MAN]。
- **随 App 注册**：macOS 13+ 用 `SMAppService.agent(plistName:)`，plist 须在 App 的 `Contents/Library/LaunchAgents`，`register()` / `unregister()` 取代手动往 `~/Library/LaunchAgents` 放 plist [SMAPP]；状态 `requiresApproval` 表示需用户在系统设置里放行，用户撤销同意时也返回此状态 [SMAPP-RA]。旧式手放 plist 应加 `AssociatedBundleIdentifiers` 以在“登录项”界面归属到 App [LAUNCHD-MAN]。
- **文档状态**：Apple 的 Daemons and Services Programming Guide 位于文档归档区，最后更新 2016-09-13 [APPLE-LAUNCHD]；以本机 man 页为准。

### 4.2 对比与推断

| 方面 | launchd Agent | 桌面应用托管子进程 |
|---|---|---|
| 生命周期 | 独立于任何窗口；登录即可常驻；脚本/agent/Notebook 随时可连 | 跟随应用；应用退出数据服务即停 |
| 崩溃重启 | 系统内置（`KeepAlive` 条件），10 秒节流 | 需自写监督逻辑 |
| 分发 | 开发期可直接写 plist、无需签名（推断：个人自用不分发即可）；随 App 注册需 macOS 13+ 与用户放行 | 对外分发需签名公证 |
| 资源 | 默认被节流，需显式设 `ProcessType` | 子进程继承应用优先级（推断） |

**推断**：需求里“一两行代码就能取到数据，不用先启动一堆东西”与“同一份数据同时供图表、策略脚本、agent”两点，都要求数据服务的生命周期**独立于任何前端窗口**。这与 launchd Agent 的模型吻合，与“桌面应用托管子进程”冲突（关掉图表窗口不应断掉策略取数）。单写者约束（ADR-0006/0011/0012）也意味着同一时刻只能有一个服务进程，launchd 的单 Label 单实例天然满足；桌面壳托管时则要防止用户同时开 `talaria up` 与桌面应用导致两个写者。

---

## 5. 开源交易软件的做法（事实）

| 项目 | 形态 | 来源 |
|---|---|---|
| Freqtrade + FreqUI | 交易机器人内置 REST/WebSocket API 服务，默认 `127.0.0.1:8080`，UI 在 `localhost:8080/trade` 打开；官方“强烈建议不要暴露到公网”；多个机器人可通过 CORS 合并进一个 FreqUI | [FT-API] |
| Jesse | `jesse run` 启动 Uvicorn 服务（默认端口 9000），在浏览器中打开仪表盘；依赖 PostgreSQL | [JESSE] |

- **推断**：两者都是“单 Python 服务 + 浏览器前端”，不做原生多窗口；Freqtrade 的“一个 UI 连多个后端”是反向形态（多服务一前端）。Freqtrade 的源码许可边界见 [copilot-trading-workbench-sources.md](./copilot-trading-workbench-sources.md)，本文只借鉴形态，不涉及代码。
- **查不到**：有官方源码可核实、且采用“原生多窗口桌面壳 + 独立数据守护进程”的开源看盘软件，本次未找到。

---

## 6. 对 Talaria 的启示（推断，供决策参考，不做最终选择）

三种形态都以“数据服务独立常驻、所有前端和脚本作为它的客户端”为前提，区别在前端承载和进程托管方式。

### 形态 A：launchd 常驻服务 + 浏览器多页面（可选 PWA）

- 做法：`talaria` 数据服务注册为用户级 LaunchAgent（`KeepAlive` 条件重启、显式 `ProcessType`）；现有 FastAPI 继续同源托管 SPA，图表、快讯、预警、扫描各给一个可直达的路由，在浏览器新标签页/窗口打开；页面间用 `BroadcastChannel` 联动；需要独立窗口观感时加 manifest，用 Chrome“安装”或 Safari“添加到程序坞”。
- 得：前端零改造、无签名公证负担、与 dsh Web 插槽同源同代码；Python 脚本与 agent 随时可连。
- 失：没有原生菜单/全局快捷键/窗口布局恢复；窗口管理依赖浏览器；Safari 添加到程序坞后本地存储与 Safari 分离。

### 形态 B：launchd 常驻服务 + Tauri v2 薄壳多窗口

- 做法：数据服务同 A，由 launchd 管理；Tauri 应用只负责窗口（每个 feature 一个 `WebviewWindow`，加载 `http://127.0.0.1:<port>/desk/<feature>`），不托管 Python 进程，必要时用 `SMAppService` 注册 Agent。
- 得：原生多窗口与窗口状态可自管；体积小；进程管理仍交给系统。
- 失：多一个 Rust 工程与构建链；WKWebView 与开发浏览器的 WebKit 差异需验证；如要给他人用需 Developer ID 签名与公证（个人自用可 ad-hoc）。

### 形态 C：桌面应用（Tauri sidecar 或 Electron）托管 Python 服务

- 做法：桌面应用启动时拉起 PyInstaller 打包的服务，退出时终止；多窗口由应用管理。
- 得：一次点击全部启动，最接近 TradingView 桌面版的体验。
- 失：数据服务生命周期绑在窗口应用上，与“脚本一两行取数、不先启动一堆东西”冲突；崩溃重启、单写者互斥（防止与 `talaria up` 并存）都要自写；Tauri 只在正常退出时清理 JS 端 spawn 的子进程，Electron 需全部自写；Electron 自带 Chromium，官方无体积数据。

**取舍要点**：选择主要取决于两个问题——(1) 数据服务是否应在没有任何窗口时也运行（是 → A 或 B）；(2) 原生窗口管理是否值得一套新的构建与签名链（否 → A）。A 与 B 共享同一后端和前端，可以先 A 后 B，后加的壳不影响已有的脚本和 agent 接入。

---

## 7. 来源

- **[TV-MW]** TradingView 帮助中心，How to open multiple app windows?：https://www.tradingview.com/support/solutions/43000611543-how-to-open-multiple-app-windows/
- **[TV-BLOG-MM]** TradingView 博客，Multi-monitor support now free on desktop app（2022-04-27）：https://www.tradingview.com/blog/en/multi-monitor-support-free-on-desktop-app-31209/
- **[TV-BLOG-XH]** TradingView 博客，Crosshair syncing now available in TradingView Desktop app（2022-03-24）：https://www.tradingview.com/blog/en/crosshair-syncing-in-tradingview-desktop-app-30157/
- **[TV-BLOG-TG]** TradingView 博客，Meet tab groups in TradingView Desktop：https://www.tradingview.com/blog/en/tab-groups-tradingview-desktop-62832/
- **[TV-RN]** TradingView Desktop releases and release notes（核查时最新 3.5.1，2026-11-17）：https://www.tradingview.com/support/solutions/43000673888-tradingview-desktop-releases-and-release-notes/
- **[TV-PINE]** I want to edit Pine code on a separate page：https://www.tradingview.com/support/solutions/43000660019-i-want-to-edit-pine-code-on-a-separate-page/
- **[TV-LAYOUT]** Layouts, charts, drawings, indicators, and their interaction：https://www.tradingview.com/support/solutions/43000692404-layouts-charts-drawings-indicators-and-their-interaction/
- **[TV-DF]** Advanced Charts 文档，Datafeed API（latest，版本选择器列 v26–v32）：https://www.tradingview.com/charting-library-docs/latest/connecting_data/datafeed-api/
- **[TV-DF-REQ]** Datafeed API Required methods：https://www.tradingview.com/charting-library-docs/latest/connecting_data/datafeed-api/required-methods/
- **[TV-MCL]** Trading Platform，Multiple-chart layout：https://www.tradingview.com/charting-library-docs/latest/trading_terminal/multiple-chart-layout/
- **[MDN-BC]** MDN，BroadcastChannel：https://developer.mozilla.org/en-US/docs/Web/API/BroadcastChannel
- **[MDN-SW]** MDN，SharedWorker：https://developer.mozilla.org/en-US/docs/Web/API/SharedWorker
- **[BCD]** mdn/browser-compat-data，提交 `bd1e5ecc5c45c2b84ef6c5ca72835c677053842b`：`api/SharedWorker.json`、`api/BroadcastChannel.json`：https://github.com/mdn/browser-compat-data/tree/bd1e5ecc5c45c2b84ef6c5ca72835c677053842b/api
- **[MDN-PWA]** MDN，Making PWAs installable：https://developer.mozilla.org/en-US/docs/Web/Progressive_web_apps/Guides/Making_PWAs_installable
- **[WEBDEV-PWA]** web.dev，Learn PWA: Installation：https://web.dev/learn/pwa/installation
- **[WEBKIT-17]** WebKit 博客，WebKit Features in Safari 17.0：https://webkit.org/blog/14445/webkit-features-in-safari-17-0/
- **[TAURI-START]** Tauri v2，What is Tauri?：https://v2.tauri.app/start/
- **[TAURI-WV]** Tauri v2，Webview Versions：https://v2.tauri.app/reference/webview-versions/
- **[TAURI-WVW]** Tauri v2 JS API，webviewWindow：https://v2.tauri.app/reference/javascript/api/namespacewebviewwindow/
- **[TAURI-CAP]** Tauri v2，Capabilities for Different Windows and Platforms：https://v2.tauri.app/learn/security/capabilities-for-windows-and-platforms/
- **[TAURI-SIDECAR]** Tauri v2，Embedding External Binaries：https://v2.tauri.app/develop/sidecar/
- **[TAURI-SHELL-SRC]** tauri-apps/plugins-workspace 提交 `baf8b014847425062cdc592e7ed567717b4b1510`，`plugins/shell/src/lib.rs` L163–L174 与 `plugins/shell/src/commands.rs` L260：https://github.com/tauri-apps/plugins-workspace/blob/baf8b014847425062cdc592e7ed567717b4b1510/plugins/shell/src/lib.rs#L163-L174
- **[TAURI-SIGN]** Tauri v2，macOS Code Signing：https://v2.tauri.app/distribute/sign/macos/
- **[EL-PM]** Electron，Process Model：https://www.electronjs.org/docs/latest/tutorial/process-model
- **[EL-UP]** Electron，utilityProcess：https://www.electronjs.org/docs/latest/api/utility-process
- **[EL-APP]** Electron，app（`setLoginItemSettings`、`window-all-closed`）：https://www.electronjs.org/docs/latest/api/app
- **[EL-SIGN]** Electron，Code Signing：https://www.electronjs.org/docs/latest/tutorial/code-signing
- **[EL-PERF]** Electron，Performance：https://www.electronjs.org/docs/latest/tutorial/performance
- **[LAUNCHD-MAN]** 本机 `man 5 launchd.plist`（macOS 27.2.1，build 26B131）：`KeepAlive`、`RunAtLoad`、`ExitTimeOut`、`ThrottleInterval`、`ProcessType`、`AbandonProcessGroup`、`AssociatedBundleIdentifiers`、CAVEATS、DEPENDENCIES 各节
- **[APPLE-LAUNCHD]** Apple Documentation Archive，Creating Launch Daemons and Agents（2016-09-13）：https://developer.apple.com/library/archive/documentation/MacOSX/Conceptual/BPSystemStartup/Chapters/CreatingLaunchdJobs.html
- **[SMAPP]** Apple，SMAppService 与 `agent(plistName:)`（macOS 13.0+）：https://developer.apple.com/documentation/servicemanagement/smappservice
- **[SMAPP-RA]** Apple，SMAppService.Status.requiresApproval：https://developer.apple.com/documentation/servicemanagement/smappservice/status-swift.enum/requiresapproval
- **[FT-API]** Freqtrade 文档，REST API & FreqUI（stable）：https://www.freqtrade.io/en/stable/rest-api/
- **[JESSE]** Jesse 文档，Getting started：https://docs.jesse.trade/docs/getting-started/
