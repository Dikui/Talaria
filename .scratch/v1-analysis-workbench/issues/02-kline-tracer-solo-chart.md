# 02 — 最窄贯通：Bybit 历史 K 线回填、证据快照冻结与 `--solo` 看盘页显示 K 线

**What to build:** 交易员执行 `talaria up --solo`，浏览器打开本地看盘页，选择一个 `Bybit V5 Linear` 永续 `Instrument` 和周期后看到由 Python 守护进程回填的历史 K 线，向左拖动时分页加载更早数据；开发者可按截止时刻冻结 `Evidence Snapshot` 并按 `snapshot_id` 读回同一份 K 线。这是贯穿契约、连接器、存储、快照、快通道接口与图表的第一条最窄路径，后续任务都在它上面加宽。

**Blocked by:** 01 — 核实并锁定外部依赖版本与许可证（只需第 2 项 KLineChart 结论）

**Source of truth:** [需求文档](../spec.md)、[领域词汇表](../../../GLOSSARY.md)、[系统分层架构规范](../../../docs/architecture.md)、[ADR-0001](../../../docs/adr/0001-dual-plane-dsh-and-python-core.md)、[ADR-0002](../../../docs/adr/0002-capability-traits-and-market-packs.md)、[ADR-0005](../../../docs/adr/0005-fast-path-bypass-and-decoupled-chart-workbench.md)、[ADR-0008](../../../docs/adr/0008-analysis-first-v1-and-deterministic-key-level-engine.md)

**Status:** ready-for-agent

- [ ] 跨语言契约检查命令可独立运行并断言：价格、数量、费率在 JSON Schema 中一律为十进制字符串；Python 导出契约与前端生成类型零漂移；面向智能体工具导出的 Schema 子集不含根级 `$schema`、无未展开 `$defs`。
- [ ] 底座契约表达五重时点（`Event Time`、`ts_published`、`Available Time` + 精度等级、`ts_ingested`、`Committed Time`）、通用 `Instrument` 规格、五项能力特征接口，以及 K 线柱状态（正常 / `NO_TRADES` / `FEED_GAP`）；核心层无特定市场条件分支，`Bybit V5 Linear` 连接器与 `24x7` `SessionTrait` 通过加密永续 `Market Pack` 注册。
- [ ] 连接器免密钥分页回填多周期 K 线；数据以最简单的方式落盘（冷库原子发布流程由 06 补齐）。
- [ ] `get_snapshot` 按截止时刻冻结 `Evidence Snapshot`，`HISTORICAL_PIT` 查询只返回 `Available Time <= cutoff_ts` 的数据并输出紧凑交易柱序列；`OBSERVED_REPLAY` 参数存在但返回明确的未实现错误；`Catalyst Calendar` 查询为空实现并遵守防前视裁剪语义。
- [ ] `talaria up --solo` 启动仅绑定 `127.0.0.1` 的守护进程并托管纯 React + `KLineChart v10` 页面；写操作接口校验启动时随机生成的会话 Bearer Token（缺失或错误返回 `401`）；收到退出信号时优雅退出。
- [ ] 图表通过历史分页接口加载 K 线，十进制字符串只在画布绘制最后一跳转为浮点数。
- [ ] 离线确定性样本测试覆盖时点截断、快照读回与鉴权；访问真实 Bybit 在线接口的连通性检查标记为沙箱外可选冒烟验证。
- [ ] 浏览器界面验收（均附截图）：宽度 `1440` 与 `390`、浅色与深色、简体中文下检查 K 线主图与标的/周期切换；覆盖长合约标识不溢出，以及尚未加载到历史行情时的空数据引导态。
