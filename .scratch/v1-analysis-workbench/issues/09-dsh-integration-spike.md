# 09 — DeepSeek Harness 接入预研：`talaria up` 双进程、同屏嵌入看盘组件与第一个只读工具

**What to build:** 尽早验证 M2 的前提是否成立：执行 `talaria up` 同时启动 Python 守护进程与 `talaria-desk` 协同宿主，看盘组件包嵌入 DSH 同屏插槽并正常显示 K 线，智能体能通过受控桥调用 `get_snapshot` 读到冻结快照。若 DSH 0.x 接口不支持嵌入或桥接，在本任务暴露并回改架构规范 / ADR，而不是等到 M1 做完。

**Blocked by:** 01 — 核实并锁定外部依赖版本与许可证（第 1 项 DSH 结论）；02 — 最窄贯通：Bybit 历史 K 线回填、证据快照冻结与 `--solo` 看盘页显示 K 线

**Source of truth:** [需求文档](../spec.md)、[领域词汇表](../../../GLOSSARY.md)、[系统分层架构规范](../../../docs/architecture.md)、[ADR-0001](../../../docs/adr/0001-dual-plane-dsh-and-python-core.md)、[ADR-0005](../../../docs/adr/0005-fast-path-bypass-and-decoupled-chart-workbench.md)

**Status:** ready-for-agent

- [ ] `talaria up` 纳管双进程健康探针、日志分流与优雅退出；`--solo` 行为不变。
- [ ] 看盘组件包同一份代码既可由快通道独立打开，也可嵌入 DSH 插槽（不引入 SolidJS 外围封装）。
- [ ] 受控桥把 `get_snapshot` 封装为符合 DSH Schema 校验子集的工具，智能体调用后返回绑定 `snapshot_id` 的结果。
- [ ] `talaria-desk` Profile 在配置补丁层移除任何下单或绕过人工确认的执行工具，挂载 DSH 原生单调执行守卫（拦截从历史轨迹 `fork` 发起的重放写操作）；`dsh --dump-config` 快照测试固化权限边界。
- [ ] DSH 宿主停止或卡住时，浏览器经快通道的切图与画线不受影响（以手动或脚本验证并记录）。
- [ ] 离线测试用模拟 / 录制的桥接消息验证工具契约；真实模型会话列为沙箱外可选冒烟验证。
- [ ] 结论（可行 / 需调整的接口与位置）追加到本任务单 `## Comments`。
- [ ] 浏览器界面验收（均附截图）：宽度 `1440` 与 `390`、浅色与深色、简体中文下检查同屏布局（图表区 + 协同面板）与宿主离线时的非阻塞提示。

## Comments
