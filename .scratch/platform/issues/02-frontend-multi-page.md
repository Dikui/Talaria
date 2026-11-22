# 02 — 功能面板进 DSH，兜底页面复用同一批组件

**What to build:** 图表、预警、扫描、关键位各做成一个独立的功能组件。在 DSH Web 里，每个组件注册为一个全局面板（root 作用域 `main` keyed slot，`ctx.layout.selectPanel(id)` 切换），交易员从 DSH 侧栏或命令进入。同一批组件也挂在服务托管的独立路由上，作为 DSH 离线时的兜底入口。在任一面板切换标的或周期，其他已打开的面板（DSH 内、其他 DSH 标签页、兜底页面）都跟着联动（`BroadcastChannel`）。

**Blocked by:** None — can start immediately.

**Source of truth:** ADR-0020（取代 ADR-0014 的前端一条）、ADR-0005；`docs/architecture.md` §1.1。

**Status:** ready-for-agent

**2026-11-22 改写：** 原单是“浏览器多页面 + PWA”为主、DSH 插槽为另一使用方；按 ADR-0020 改为 DSH 为主入口。

- [ ] 组件边界：`talaria-desk-ui` 中每个功能组件只从 props 和自己的 hooks 取状态，不 import 任何 DSH 包；DSH 胶水只在 `talaria-dsh-plugin/client`。加一条静态检查（如 import 边界测试）保证这一点。
- [ ] DSH 面板：图表、预警、扫描、关键位四个面板注册到 `main` 插槽，可从 DSH 侧栏进入；会话右栏里现有的图表 tab 不受影响。面板内的行情、历史分页、画线、预警写操作直连服务快通道，不经 DSH 宿主转发（看网络请求确认）。
- [ ] 兜底页面：服务托管四个独立路由，原 `/desk` 入口保留并链接到各页面；只打开其中一个时也能完整工作。不做 Web App Manifest。
- [ ] 状态恢复：DSH 刷新后布局会重置，Talaria 自己在浏览器本地记住当前面板、标的和周期，刷新后恢复。
- [ ] 联动：DSH 内两个面板之间、两个 DSH 标签页之间、DSH 与兜底页面之间，切换标的/周期互相同步，十字光标时刻同步；关闭任一方不影响其他。
- [ ] 降级：停掉 DSH 宿主后，兜底页面照常看盘、画线、管理预警。
- [ ] 浏览器检查并截图：DSH 面板与兜底页面各一组，宽度 1440 与 390、浅色与深色主题；空数据（无自选、无预警）与长标的名称；两处联动的前后截图。
