# 设计人机共享图表画线状态与双轨协同交互协议

Labels: wayfinder:grilling
Type: grilling
Status: resolved
Blocked by: 01

## Question

围绕 `KLineChart v10` 核心库与 DeepSeek Harness 工作台（`ui-slots` / `typert`），如何设计“人工交易员 + 智能体”同台看盘、共享标注与双向交互的完整契约？具体需敲定：
1. 人机共享的图表 `Overlay` 画线与标注数据模型（如何区分人工手绘线、智能体建议标注线与活跃仓位的止损/止盈屏障线，以及智能体画线写入前的真实 K 线价格范围硬校验规则）；
2. 双轨交互的三种核心流转协议：① 人工在图表框选区间或针对当前自选标的发起智能体局部会诊的数据包格式；② 智能体推送交易方案草稿（`staged` 暂存态）由人工在界面拖动/修改入场或止损参数后确认送入模拟盘的状态流转；③ 人工独立看盘、画线与下单通道完全绕开大模型等待的非阻塞架构约束。

## Answer

按推荐方案定型基于 `KLineChart v10` 核心库（Apache-2.0，不引入 SolidJS 版 `@klinecharts/pro` 外壳，改用零 DSH 依赖的纯 React 组件包 `talaria-desk-ui` 自建薄工具栏）的人机共享画线状态与双轨协同交互契约：

### 1. 三类图层物理分离的 `Overlay` 模型与智能体防幻觉硬门控

前端画布将所有视觉图元严格划分为三个独立 `groupId` 图层，职责与写入权限互不混杂：

1. **人工结构画线层（`HUMAN_DRAWING`，持久化于 Python L1 `overlay_store`）**：
   - 覆盖水平支撑阻力、射线、线段、平行通道、斐波那契回撤、矩形区间与文本标签；标准契约 `OverlayRecord` 包含 `overlay_id`、`instrument_id`、`timeframe_scope`（指定单周期可见或跨周期共享）、`template_name`、`points: list[{ts_ms, price: DecimalStr}]`、`author: "HUMAN"`、`version`、`ts_committed` 与软删除 `deleted_at`（保留版本链供历史快照与撤销重做栈使用）。
   - 支持自由拖拽锚点、样式编辑及一键绑定 `OVERLAY_CROSS` 盘面预警。
2. **智能体建议标注层（`AGENT_ANNOTATION`，经硬门控校验后持久化于 L1 `overlay_store`）**：
   - `author: "AGENT"`，必填关联 `snapshot_id`、`dsh_turn_id` 与非空 `evidence_refs`（如引用的历史极值 K 线时间戳或事件 ID）；视觉上默认以带 AI 角标的虚线/半透明区间带渲染且默认锁定防误触，提供人工一键“采纳转为人工实线”、“按会话显隐过滤”或“批量清理”操作，同时限制单标的未采纳活跃智能体标注条数上限（默认 `<= 12` 条，旧轮次自动归档隐藏）防止画布污染。
   - **写入前确定性硬门控（洁净室深化价格区间与溯源校验）**：Python 写入接口对智能体提交的 `OverlayRecord` 强制执行三项校验，任一不通过立即返回结构化校验错误供模型自纠或弃权：① 引用的 `snapshot_id` 必须真实存在且归属同一 `instrument_id`；② 所有价格坐标 `price` 必须为正、符合标的 `tick_size` 步长，且落在所引用快照近期分析窗口 `[min_low - 3 * ATR_14, max_high + 3 * ATR_14]` 合理包络内，直接拦截越界幻觉价位；③ 历史时间锚点不得早于快照起始时间，向右侧未来延伸的推演路径锚点跨度不得超过当前周期 100 根 K 线。
3. **计划与持仓屏障投影层（`EXECUTION_BARRIER`，实时状态单向投影，不混存进普通画线表）**：
   - 展示 `StagedPlanDraft`（待确认协同草案）的盈亏比尺及 `PositionExecutor` 活跃计划的入场挂单带、硬止损线、止盈线、激活式移动止损线与到期垂直线。
   - 该图层直接由后端草案/执行状态经 WebSocket 实时投影生成；人工在图表上拖动此图层的止损/入场手柄时，触发的不是普通画线保存，而是“草案参数重算”或“活跃仓位屏障修改命令”，从根源消除“图上的止损线改了但后台执行器没改”的状态脱节。

### 2. 双轨协同三类核心流转协议与三来源归因边界

1. **人工局部框选与标的会诊协议（`ConsultationPacket`）**：
   - 交易员在 `KLineChart` 框选局部时间-价格窗口或点击“请求协同会诊”时，前端先调用 Python 本地快通道冻结此刻 `snapshot_id`，再自动拼装结构化 `ConsultationPacket`（包含 `instrument_id`、当前周期、视口时间范围、框选焦点区间 `focus_range`、当前有效 `HUMAN_DRAWING` 列表、窗口内待公布/刚公布 `CatalystEvent`、当前目标子账户 `sub_account_id` 可用风险预算摘要及交易员自然语言问题），经 DSH `typert` 发送给智能体会话；智能体通过 `get_chart_view` / 快照读取工具对齐同一截面，杜绝跨时间错位回答。
2. **暂存交易草案（`StagedPlanDraft`）交互流转与严格三来源归因（`DecisionOrigin`）**：
   - 在双轨协同模式下，智能体生成的 `SetupInsight` 一律先写入 Python L2 `plan_drafter` 生成 `StagedPlanDraft`（状态机：`STAGED` → `MODIFIED` → `SUBMITTED` | `DISCARDED` | `EXPIRED`），通过本地 WebSocket 即刻渲染在图表上，并调用 L3 无副作用试算接口 `preview_sizing()` 在线旁实时标注“目标子账户 `1R` 对应数量、预估强平价距离与净赔率”。
   - 交易员可直接在图表拖动入场带或止损/止盈线微调（前端每次松手毫秒级刷新 `preview_sizing` 试算结果），确认无误后点击“提交执行计划”送入 L3 正式定仓风控。
   - **互斥且可审计的 `DecisionOrigin` 判定准则**：
     - `HUMAN`：由交易员在图表多空尺或下单面板从零独立创建并提交，未引用任何智能体 `StagedPlanDraft` 或会诊回合；
     - `CO_PILOT`：由智能体生成 `StagedPlanDraft` 后经交易员审阅确认/拖动修改提交，或由交易员发起 `ConsultationPacket` 会诊后关联该回合建议提交（同时保留智能体原始草案参数快照，供盘后计算人工修订边际贡献 `Co-Pilot Lift`）；
     - `AGENT`：绑定在智能体自治虚拟子账户上、由 `TriggerEvent` 唤醒后不经人工逐笔干预直接送入 L3→L4 执行的全自动计划。
3. **人工通道零大模型阻塞硬约束**：
   - `KLineChart v10` 的 `setDataLoader` 历史分页、实时增量订阅、鼠标画线乐观渲染（拖拽过程纯前端 `60fps` Canvas 重绘，`pointerup` 后经本地 HTTP 快通道异步落库）、`preview_sizing` 试算、手动开仓提交及紧急平仓撤单全部直连 Python 本地 `127.0.0.1` 快通道，链路上不包含任何等待 DSH 宿主或大模型响应的同步调用；当 DSH 未启动、断连或正在长耗时推理时，除右侧协同对话区显示忙碌/离线状态外，全部人工看盘与模拟交易功能保持毫秒级可用。


## Comments

### 交接前整体复查修订（覆盖已解决 5、8 张）
- **原因**：独立复查指出需明确 `talaria-desk` 协同会话与自治子账户的工具权限边界，以及 `ConsultationPacket` 场景下无结构化草案时的归因细则。
- **修订结论**：
  1. `talaria-desk`（Web 协同 Profile）在插件配置层物理移除直接提交开仓计划工具，仅暴露只读快照/算子、`write_agent_overlay` 与 `stage_plan_draft`；`CO_PILOT_CONSULT` 预警经前端 WebSocket 推送会诊提醒卡，由人工一键带入会话或按配置触发草案生成。
  2. 纯问答式 `ConsultationPacket` 若未产生结构化 `StagedPlanDraft`，关联交易仍记 `DecisionOrigin = CO_PILOT`，同时标记 `lift_applicable = False`（不强行构造虚拟基准单），其消耗的 Token 照常计入目标子账户认知推理成本。
