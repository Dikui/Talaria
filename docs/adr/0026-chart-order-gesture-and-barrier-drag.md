---
status: accepted
---

# 图表下单临时手势与活跃屏障拖拽交互边界

2026-11 决定：在独立看盘工作台（`@luxalgo/vela`）的图表交易闭环中，将**临时下单手势**与**活跃回合屏障拖拽**拆分为两条严格隔离于 `HUMAN_DRAWING` 持久化画线存储的交互通道，同时满足 `< 5 ms` 拖动实时预演（[ADR-0010](./0010-pure-python-indicator-core.md)）、零画线污染、松手二次确认防误触（spec D10）以及 WebGL2/Canvas2D 降级兼容（[ADR-0023](./0023-standalone-workbench-and-rust-data-engine.md)）。

## 决策内容

1. **图上新建草案使用 Vela 内置 `'position'` 作为不落库的临时手势（D6）**：
   - 交易员触发做多/做空下单手势时复用 `@luxalgo/vela` 内置 `'position'` 绘图，由 `VelaDrawingSyncBridge` 按 `type === "position"`（含 `ephemeralIds` 追踪）全面拦截，**绝不写入 `HUMAN_DRAWING` 存储**，也不混入画线版本链。
   - 挂载时强制关闭 Vela 内置伪定仓文案（`showLossSize: false`），避免其硬编码假定账户余额与 Talaria Rust/Python 权威定仓预演冲突。
   - 放置阶段通过 `drawing:draft` 驱动节流定仓预演；已放置后的句柄/整体拖拽期，因 Vela 仅在松手时触发 `drawing:edited`，在外层容器挂载只读被动捕获的 **Shadow Handle-Drag Tracker** 实时将光标像素转为价格并镜像锚点约束，以 `< 5 ms` 延迟驱动 Rust 定仓预演与浮层回显。
   - 点击“生成草案”将单入场价与单止盈映射为预填 `DraftForm`（多止盈 TP2–TP4 与入场带宽度在表单补填，舍入后校验严格几何分离）；手势结束、取消或切换标的/周期/进入回放时显式清理，并通过静默 `fromJSON` 清空含临时 `'position'` 的画线撤销栈，防止 `Ctrl+Z` 复活已清理手势。

2. **活跃回合屏障拖拽采用自定义 `EXECUTION_BARRIER` 渲染层 + 外层容器捕获拖拽（方案 B）**：
   - 活跃回合的止损与止盈屏障仅作为只读投影绘制在原生渲染层 `EXECUTION_BARRIER`（配合未终结草案的只读层 `LAYER_STAGED_DRAFT`），**不创建任何 Vela 绘图对象**，与 `DrawingStore` / `DrawingHistory` 彻底隔离。
   - 在图表外层容器（而非内部 `dataCanvas`）绑定捕获阶段 `pointerdown`，并在拖拽期间于 `window` 绑定 `pointermove` / `pointerup` / `pointercancel` / `Escape`；确保 WebGL2 上下文丢失触发 `swapToCanvas2dFallback` 替换 `dataCanvas` 后拖拽无缝存活，且命中屏障时阻断底层平移与画线选中。
   - 多子账户同价带屏障采用共享纯函数 `layoutBarrierTargets` 水平错峰排布标签 pill，并执行 **pill 矩形（X+Y）优先于线条本体** 的命中测试，避免重叠遮挡。
   - 离线、回放中或账户处于 `HALTED` 状态，以及画线工具/橡皮擦激活或光标在坐标轴区域时，自动放行或禁用拖拽并展示只读提示。

3. **松手二次确认与乐观态超时收敛（D10）**：
   - 拖拽超过防误触阈值松手后不立即发送命令，而是保持原价鬼影线与目标价预览线进入本地 `pending_confirm` 态并弹出确认框（展示操作分类、是否直达成交插槽或须过风控插槽）；取消即刻清态归位，零命令发出。
   - 确认后携带 `based_on_ledger_seq` 提交 `PaperCommand`（[ADR-0025](./0025-paper-account-projection-and-intent-queues.md)）并进入 `pending_ack` 虚线态，同时启动 `10s` 超时定时器；收到权威投影更新、命令回执（含拒单或 `COMMAND_STALE`）或超时后立即收敛或回退至权威投影。

## Considered Options

- **将图上仓位框直接存入 `HUMAN_DRAWING`**：未确认的临时下单意图会污染持久化画线版本链、触发预警动作并混入协同视图。否决。
- **方案 A：用 Vela 临时 `hline` 绘图承载每条活跃屏障的拖拽**：原型实测确认四项结构性缺陷——① `DrawingInteraction.move()` 拖动中不发事件，仍需外挂 DOM 追踪；② 松手即写入 `DrawingStore` 并压入 undo 栈，与 D10 取消回退和 `Ctrl+Z` 冲突，且暴露画线工具栏/橡皮擦/Delete 误删面；③ `HorizontalLine.hitTest` 忽略 X 坐标，多子账户同价屏障下层永远拖不到；④ 设 `locked: true` 会同时禁止拖拽。否决。
- **拖拽屏障松手即刻执行（不弹窗确认）**：易在看盘缩放或误拖时意外改单，尤其是放宽止损类高风险操作。否决（采纳 D10）。
- **将捕获监听器直接绑定在 Vela 内部 `dataCanvas`**：WebGL2 降级为 Canvas2D 时会替换 `dataCanvas` 节点，导致监听器静默失效。否决（改绑外层容器）。

## Consequences

- 仅使用 Apache-2.0 许可的 `@luxalgo/vela` 与 `@luxalgo/vela/plugin`，继续严守 [ADR-0023](./0023-standalone-workbench-and-rust-data-engine.md) 的许可证与窄接口边界。
- 图表交易手势与屏障拖拽均保持零 `HUMAN_DRAWING` 写入与零 `requestAnimationFrame` 轮询，坐标投影直接复用 Vela 原生 `CoordinateSystem`。
- 已知权衡：临时 `'position'` 清场时调用 `fromJSON` 会重置该图表当前会话的画线 undo 栈（已持久化的画线版本链不受影响），以此换取零魔改 Vela 私有历史实现。

依据：`.scratch/chain3-paper-trading/spec.md`（D6、D10，仅本地）；`.scratch/chain3-paper-trading/issues/06-prototype-chart-gestures.md`（仅本地）。
