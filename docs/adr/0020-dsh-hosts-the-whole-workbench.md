---
status: superseded by ADR-0023
---

> **已被取代（2026-11）：** 请参阅 [ADR-0023](./0023-standalone-workbench-and-rust-data-engine.md)。看盘工作台已迁出 `dsh/` 成为独立一等产品（`workbench/`，`Tauri v2` + `@luxalgo/vela` + `/desk` 同构路由），不再以 DSH 作为工作台主宿主；DSH 插件仅作为可选 `Co-Pilot Analysis` 消费方。

# 整个工作台放进 DSH，浏览器多页面只作离线兜底

2026-11-22 决定：交易员日常只从 DSH Web 进入 Talaria。图表、预警、扫描、关键位等功能页都注册为 DSH 的全局面板（root 作用域 `main` keyed slot，经 `ctx.layout.selectPanel(id)` 切换）；会诊时需要的图表仍放在会话右栏（`ui-sidebar-right` 的 tab）。DSH 继续作为 agent 宿主：会诊、简报和 7 个分析工具留在 `talaria-dsh-plugin`，不再迁进服务。

本 ADR 取代 ADR-0014 中“前端”一条，以及 2026-11-21 讨论中“DSH 不保留特殊地位、替换完成后删除 DSH 专用代码”的待定方向。ADR-0014 的常驻服务部分、ADR-0005 的快通道都不变：

- 功能面板里的行情、历史分页、画线、预警和草案写操作仍由浏览器直连 `127.0.0.1` 上的常驻服务，不经 DSH 宿主转发。DSH 只提供外壳、会话、轨迹和审批。
- 服务托管的浏览器多页面（`/desk` 及各功能路由）保留，用作 DSH 未启动、崩溃或升级出问题时的兜底入口。两处复用 `talaria-desk-ui` 的同一批 React 组件，各功能组件不依赖 DSH 的任何 API；DSH 专用的胶水只放在 `talaria-dsh-plugin/client`。
- agent 工具的底层仍是服务的 HTTP 接口，SDK 与 Notebook 照常调用，不经 DSH。

**2026-11-24 修订（导航与语言）：** 将上述「每页一个全局面板」改为一个 Talaria 全局面板，图表、预警、扫描、关键位和账户在面板内用标签切换，避免与宿主功能混排。Talaria 恢复上次选中的标签，保留会话右栏面板和原有独立 `/desk/{功能}` 地址。DSH 内的工作台语言跟随宿主；独立页面跟随浏览器，支持链接以 `?lang=en` 或 `?lang=zh` 指定显示语言。不改变业务标识、数据、请求协议和跨窗口联动。

## Considered Options

- **维持 ADR-0014：浏览器多页面为主，DSH 只是使用方之一，最终删掉 DSH 专用代码**：看盘和会诊分在两个窗口，交易员要在两处切换，会诊时引用的图表与主图表脱节。否决。
- **所有前端流量都经 DSH 宿主中转**：浏览器只连 DSH 一个端口，但模型推理占用 Node 事件循环时会卡住切图、画线和紧急平仓，DSH 预览版协议变更也会直接打断看盘。ADR-0005 已否决，这次仍否决。
- **只保留 DSH，删掉服务托管的页面**：少维护一个入口，但 DSH 0.2.x 是预览版，宿主挂了就无法看盘。否决。
- **工具定义迁进服务，DSH 只做渲染和会话**：将来换 agent 宿主更容易，但现在没有换宿主的计划，迁移只是多一层间接。否决；真要换宿主时再迁。

## Consequences

- **已由 [ADR-0021](./0021-dsh-desktop-app-as-host.md) 修订。** DSH 成为日常入口，宿主必须随服务常驻。沿用现有做法：服务以子进程方式启停 DSH 宿主（`cli/dsh_host.py`）；launchd 只管服务。DSH Web 的入口 URL 带随机 token，需要提供一个打开入口的命令（如 `talaria open`），取不到 DSH 时打开兜底页面。
- 每个功能组件必须同时能挂在 DSH 面板和独立路由上，因此组件只从 props 和自己的 hooks 取状态，不读 `ctx`。标的、周期与十字光标的联动继续用 `BroadcastChannel`，在 DSH 面板之间、DSH 与兜底页面之间都有效。
- DSH 布局状态刷新后重置（`ui-layout` 已知限制）。当前选中的面板、标的和周期由 Talaria 自己存在浏览器本地，刷新后恢复。
- 多窗口靠多开 DSH Web 标签页实现；PWA 安装只对兜底页面有意义，不再作为验收项。
- DSH 升级前除了现有的 `--dump-config` 与确定性探针，还要检查 `main` 面板插槽与右栏 tab 的注册接口没有变。
