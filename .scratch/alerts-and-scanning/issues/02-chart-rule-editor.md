# 02 — 图表规则编辑器与规则输出显示

**What to build:** 在图表页上组合出一条规则，立即在图上看到它的信号标记和引用的指标线，确认后一键存为预警。

**Blocked by:** platform/02 — 前端多页面；01 — JSON 规则与预警升级。

**Source of truth:** ADR-0019；`docs/architecture.md` §5.2、§5.5。

**Status:** ready-for-agent

- [ ] 编辑器按 JSON 规则 Schema 生成表单（算子、参数、比较、逻辑组合），不提供自由文本代码。
- [ ] 规则输出由后端计算，经 KLineChart 异步 `calc` 显示。
- [ ] 一键存为预警，预警页可见。
- [ ] 浏览器检查并截图：宽度 1440 与 390、浅色与深色；空规则、嵌套三层的长规则、引用的算子返回全 `null`（预热期）。
