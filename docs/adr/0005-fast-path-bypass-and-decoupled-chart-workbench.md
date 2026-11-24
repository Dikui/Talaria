---
status: accepted（已由 ADR-0023 修订：独立工作台迁出 dsh/ 并升级渲染核至 @luxalgo/vela）
---

# 看盘工作台采用本地快通道直连底座并与 DSH 插槽解耦

> **2026-11 演进修订（见 [ADR-0023](./0023-standalone-workbench-and-rust-data-engine.md) 与 `docs/experiments/chain1-phase0-poc.md`）：**
> - **工作台独立化**：看盘工作台已从 `dsh/packages/` 迁出为仓库一等包 `workbench/`（`Tauri v2` + React 19 + `dockview-react` + `/desk` 同构浏览器入口），不再依赖 DSH 作为主宿主。
> - **快通道直连 Rust `Resident Service`**：图表数据、实时流与画线/预警状态持久化经本地回环（`127.0.0.1`）Arrow IPC / HTTP / 双语义 WebSocket 直连 Rust `talaria-data`。
> - **渲染核升级**：通过窄接口 `ChartSurfaceAdapter` 隔离底层图表库，主选实现升级为 `@luxalgo/vela@0.10.17`（仅核心与 `plugin` SDK，Apache-2.0；严禁引入 AGPL-3.0 的 `@luxalgo/vela-pinets` 与非 React 的 `@luxalgo/vela/workspace`），按 `NOTICE` 在可见状态栏/关于面板保留 `"Vela"` 及 `https://velacharts.dev` 归属标注；通过 `chart.drawings` 承担 `HUMAN_DRAWING`，通过 `registerRendererLayer` 原生渲染 `SYSTEM_LEVEL` / `AGENT_ANNOTATION` / `EXECUTION_BARRIER` 并消除原 `requestAnimationFrame` SVG 轮询，按 `registerNativeIndicator` 直接消费外部计算序列并遵守单页 WebGL2 预算（`maxWebglCells = 8`）。原 `klinecharts 10.1.1` 仅作为 `ChartSurfaceAdapter` 的参考/可选回退边界保留。

（以下保留原 2026-11 初版决定与基线记录供溯源）

人机协同看盘前端以 `KLineChart v10`（Apache-2.0）核心库为基础，封装为零 DeepSeek Harness 内部依赖的纯 React 组件包 `talaria-desk-ui`，外覆一层极薄的 DSH 插槽适配包 `dsh-workbench`。实时行情与盘面预警推送、图表历史分页、人机画线持久化、草案定仓预试算、手动开仓提交及紧急减平仓一律经本地回环（`127.0.0.1`）HTTP/WebSocket 快通道直连 Python `talaria-daemon`，仅智能体会诊对话、`Trajectory` 思考链审阅与单次审批走 DSH 原生 `typert` 通道；同时支持在不启动 DSH 的情况下由 Python 守护进程直接托管打开看盘页面。

## Considered Options

- **所有前端流量统一经由 DSH Web 宿主（`typert` / SSE）中转至 Python**：浏览器只连 `:3080` 单端口，但高频 K 线/BBO 流须经 Node 宿主二次序列化转发，当大模型会话或 PTC 沙箱占用宿主事件循环时会阻塞人工切图、画线与紧急平仓，且在 DSH `0.2.x` 预览版发生插件协议变更时看盘链路脆弱。
- **直接采用 TradingView `lightweight-charts v5` 或引入 `@klinecharts/pro` / `FINOS Workspace`**：`lightweight-charts v5` 不内置技术指标与完整交互画线引擎且带强制公开归属外链条款；`@klinecharts/pro` 基于 SolidJS 构建外围 UI，与 React 插槽栈异构；开源后的 `FINOS Workspace` 自带重型全栈后端与独立 Copilot 协议，不适合作为轻量插件嵌入。

## Consequences

- 从物理通信路径上保证慢速大模型推理、断连或升级故障对人工看盘、画线、盘面预警及模拟交易零阻塞。
- 图表技术指标与 DSH PTC 运行时不自行计算，统一消费 Python 底座按紧凑交易柱序列契约（`SessionCompactBars`）产出的结果，从源头避免数值漂移（见 ADR-0008）。
- **图表库版本基线（2026-11-19 核实）**：锁定 `klinecharts 10.1.1` 正式版（Apache-2.0）。`registerIndicator` 不能直接注入外部结果，须注册模板并以 `createIndicator({ name, calc })` 的 `calc` 返回与 `dataList` 等长同序数组，更新时换新闭包并 `overrideIndicator`；历史分页使用 `setDataLoader`（`init / forward / backward / update`）。
