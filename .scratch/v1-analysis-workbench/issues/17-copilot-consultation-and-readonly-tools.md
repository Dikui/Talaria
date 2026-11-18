# 17 — 协同会诊：框选发起、只读分析工具链、`Abstain` 降级与 Token 留痕

**What to build:** 交易员在同屏工作台框选图表局部区间或点击“请求协同会诊”，智能体基于同一 `snapshot_id` 调用只读工具读取快照、指标、关键位与当前视窗，输出带证据引用的 `Setup Insight`；数据陈旧、证据冲突或超时时返回带原因的 `Abstain`。每轮 Token 用量由宿主侧幂等留痕。

**Blocked by:** 09 — DeepSeek Harness 接入预研；10 — 关键位引擎第一批与 `SYSTEM_LEVEL` 图层

**Source of truth:** [需求文档](../spec.md)、[领域词汇表](../../../GLOSSARY.md)、[系统分层架构规范](../../../docs/architecture.md)（§3.6）、[ADR-0001](../../../docs/adr/0001-dual-plane-dsh-and-python-core.md)、[ADR-0004](../../../docs/adr/0004-dual-mode-intraday-cognition-and-scanner-gate.md)、[ADR-0008](../../../docs/adr/0008-analysis-first-v1-and-deterministic-key-level-engine.md)

**Status:** ready-for-agent

- [ ] 在 09 的桥上补齐 `get_indicators`、`get_key_levels`、`compute_operator`、`get_chart_view` 工具，PTC 运行时绑定同口径 Python 算子；提示词按“静态分析纪律前缀 → 加密永续 `Market Pack` 领域技能 → 当前快照增量”三段组装，不接入绕过快照的外部行情源。
- [ ] 框选或点击会诊时，前端先经快通道冻结 `snapshot_id`，再拼装会诊包（`Instrument`、周期、可视范围、框选区间、有效 `HUMAN_DRAWING`、窗口内 `Catalyst Calendar` 事件（初版为空）与自然语言问题）。
- [ ] `STALE` 快照、关键证据冲突、几何关系倒挂或推理超时一律输出带原因的 `Abstain`。
- [ ] 宿主侧（非模型可调用工具）每轮结束按轮次标识幂等上报输入、缓存命中、输出 Token 并落库；守护进程可扫描本地会话轨迹补齐漏报。
- [ ] 长推理、超时或宿主被停止时，快通道切图、画线与预警提醒不受影响。
- [ ] 离线测试用模拟 / 录制桥接消息覆盖工具契约、`Abstain` 降级、Token 幂等与快通道非阻塞；真实模型会话为沙箱外可选冒烟。
- [ ] 浏览器界面验收（均附截图）：宽度 `1440` 下浅色与深色、简体中文检查框选会诊交互、长篇 `Setup Insight` 与多条证据引用折叠、未发起会诊的空会话引导态、宿主忙碌 / 离线提示；宽度 `390` 下检查会诊结果可读、框选入口隐藏。
