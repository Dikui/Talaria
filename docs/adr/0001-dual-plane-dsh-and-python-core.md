---
status: accepted
---

# 采用 DeepSeek Harness 与 Python 确定性底座分离的双平面架构

将系统拆分为两个跨进程协作的运行平面：**Plane A（认知与协同工作台平面）** 基于 DeepSeek Harness (`dsh` / Cordis 微内核，TypeScript) 插件与零宿主耦合的看盘前端组件构建，负责智能体推理编排、领域技能挂载、编程式工具调用（PTC）、前置执行守卫与人机协同界面；**Plane B（确定性数据与交易底座）** 基于 Python 模块化单体构建，独占管理时点数据快照、盘面预警与异动扫描、风险定仓、硬风控、执行状态机、多子账户权威资金账本与评测归因引擎。浏览器看盘与人工低延迟操作通过本地回环 HTTP/WebSocket 快通道直连 Plane B；人机协同会话（`talaria-desk` Web Profile）通过受控桥接工具向 Plane B 读取快照与提交暂存交易草案，智能体自治子账户（`talaria-auto`）与历史回测（`talaria-eval`）则统一由 Plane B 经官方 `deepseek-harness-sdk`（`stdio` JSON-RPC 2.0）驱动。

## Considered Options

- **全量构建在 DeepSeek Harness (TypeScript) 插件内**：直接用 TypeScript 在 DSH 进程内编写连接器、指标、回测与台账（类似社区交易插件形态）。否决原因：DSH 当前处于 `0.2.x` Developer Preview 阶段并明确提示存在兼容性破坏变更；且金融双时间列式分析（`Parquet + DuckDB`）、精确十进制保证金核算及量化寻优生态在 Python 中成熟度远高于浏览器/Node 插件环境。
- **纯 Python 单栈自建 Agent 循环而完全放弃 DSH**：在 Python 内用通用编排框架自建模型循环与会话底座，不引入 DSH。否决原因：重复建设 DSH 已原生提供的会话轨迹审计（Zstd JSONL `Trajectory`）、PTC 运行时、单调执行守卫（Monotonic Execution Guards）、闭合 `'allowed-once'` 审批链与 `ui-slots` 插槽系统（仅将看盘图表组件做成既可嵌入 DSH 插槽又可由本地快通道独立打开的纯 React 包，而非重写整套智能体底座）。

## Consequences

- **自治与回测完全同构，协同与自治权限硬隔离**：实时自治子账户与历史逐期回测均由 Python 经 `deepseek-harness-sdk` 驱动精简 `sdk-minimal` 派生 Profile；`dsh web`（`talaria-desk`）专职服务人机协同，物理不挂载跳过人工确认的自动下单工具。
- **故障与越权隔离**：即使 DSH 会话崩溃、推理超时、从历史轨迹分支（`fork`）重放、受外部新闻提示注入，或上游 `0.x` 插件 API 变动，也不会阻塞人工看盘与紧急避险，不会污染权威资金账本，更无法绕过 Python 侧硬风控。
- **契约维护要求**：跨语言边界以 Python 底座契约模型为单一事实源导出 JSON Schema 快照（金额与价格一律采用十进制字符串），由构建工具自动生成 TypeScript 类型及符合 `@deepseek-ai/dsh-mcp-client` 校验子集（剥离根级 `$schema` 与展开 `$defs`）的工具定义。
- **版本基线**：ADR-0021 将 DSH 桌面基线更新至 `dsh 0.2.2-rc.5`，Python SDK 基线为 `deepseek-harness-sdk 0.2.2rc2`；~~`ui-slots 0.0.1-rc.1` 为 BSD-3 且与 0.2.2 线的 MIT 许可存在漂移~~（2026-11-19 任务 09 实测：rc.3 安装包已自带 `ctx.slots` / `ctx.sidebarRightTabs`，看盘组件不依赖单独发布的 ui-slots 包；该包版本不再作为基线），`typert` 仍为 0.x 预览版，使用前需复核。`turn/end` 事件不含 usage 且 `deriveTurnTokenUsage` 非公开导出，认知成本采集须在 bridge 侧自行折叠 attempt 事件。
