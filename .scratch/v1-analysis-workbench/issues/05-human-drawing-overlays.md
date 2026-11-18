# 05 — 人工画线（`HUMAN_DRAWING`）持久化、撤销重做与视窗回读

**What to build:** 交易员在看盘页的 `HUMAN_DRAWING` 图层绘制和拖拽编辑结构线，松手后自动保存，刷新或切换周期后按可见范围恢复，可撤销重做；共享视窗回读接口 `get_chart_view` 返回当前视窗与其中的人工画线，供预警与智能体会诊读取。

**Blocked by:** 02 — 最窄贯通：Bybit 历史 K 线回填、证据快照冻结与 `--solo` 看盘页显示 K 线

**Source of truth:** [需求文档](../spec.md)、[领域词汇表](../../../GLOSSARY.md)、[系统分层架构规范](../../../docs/architecture.md)、[ADR-0005](../../../docs/adr/0005-fast-path-bypass-and-decoupled-chart-workbench.md)

**Status:** ready-for-agent

- [ ] 轻量 React 画线工具栏支持水平线、射线、线段、平行通道、斐波那契回撤、矩形区间与文本标签。
- [ ] 拖拽过程在前端乐观重绘，不等待后端；松手后经快通道写入协同与感知状态库（进程内单一异步串行写队列独占写入，SQLite WAL）。记录含十进制字符串价格坐标、`Instrument`、单周期 / 跨周期可见范围、递增版本号与软删除时间戳。
- [ ] 基于版本链的撤销、重做、单条删除与按当前 `Instrument` 清理；刷新或切回周期后准确恢复。
- [ ] `get_chart_view` 返回当前 `Instrument`、周期、可视时间-价格窗口及窗口内全部有效 `HUMAN_DRAWING`（本接口只在此任务实现）。
- [ ] 浏览器界面验收（均附截图）：宽度 `1440` 下浅色与深色、简体中文检查画线工具栏、锚点选中态与属性浮层，覆盖长文本标注在图表边缘不被裁切、当前 `Instrument` 无画线时的管理列表空态；宽度 `390` 下检查已有画线只读显示、工具栏隐藏。
