---
status: accepted
---

# 看盘工作台采用本地快通道直连 Python 底座并与 DSH 插槽解耦

人机协同看盘前端以 `KLineChart v10`（Apache-2.0）核心库为基础，封装为零 DeepSeek Harness 内部依赖的纯 React 组件包 `talaria-desk-ui`，外覆一层极薄的 DSH 插槽适配包 `dsh-workbench`。实时行情与盘面预警推送、图表历史分页、人机画线持久化、草案定仓预试算、手动开仓提交及紧急减平仓一律经本地回环（`127.0.0.1`）HTTP/WebSocket 快通道直连 Python `talaria-daemon`，仅智能体会诊对话、`Trajectory` 思考链审阅与单次审批走 DSH 原生 `typert` 通道；同时支持在不启动 DSH 的情况下由 Python 守护进程直接托管打开看盘页面。

## Considered Options

- **所有前端流量统一经由 DSH Web 宿主（`typert` / SSE）中转至 Python**：浏览器只连 `:3080` 单端口，但高频 K 线/BBO 流须经 Node 宿主二次序列化转发，当大模型会话或 PTC 沙箱占用宿主事件循环时会阻塞人工切图、画线与紧急平仓，且在 DSH `0.2.x` 预览版发生插件协议变更时看盘链路脆弱。
- **直接采用 TradingView `lightweight-charts v5` 或引入 `@klinecharts/pro` / `FINOS Workspace`**：`lightweight-charts v5` 不内置技术指标与完整交互画线引擎且带强制公开归属外链条款；`@klinecharts/pro` 基于 SolidJS 构建外围 UI，与 React 插槽栈异构；开源后的 `FINOS Workspace` 自带重型全栈后端与独立 Copilot 协议，不适合作为轻量插件嵌入。

## Consequences

- 从物理通信路径上保证慢速大模型推理、断连或升级故障对人工看盘、画线、盘面预警及模拟交易零阻塞。
- 图表自定义技术指标与 Python 底座、DSH PTC 运行时必须通过共享黄金测试向量及紧凑交易柱序列契约（`SessionCompactBars`）保持数值对齐。
