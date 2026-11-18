# 03 — 实时 K 线与盘口流、`NO_TRADES` / `STALE` 断流判定与顶栏健康提示

**What to build:** 看盘页的 K 线随 `Bybit V5 Linear` 实时公共流跳动，进行中 K 线与闭合 K 线、数据修订（`Revision`）正确呈现，顶栏显示最优买卖盘口；行情源在供流窗口内失联时顶栏显示断流状态与持续时长，重连补齐后自动解除。正常稀疏无成交与真实断流被严格区分。

**Blocked by:** 02 — 最窄贯通：Bybit 历史 K 线回填、证据快照冻结与 `--solo` 看盘页显示 K 线

**Source of truth:** [需求文档](../spec.md)、[领域词汇表](../../../GLOSSARY.md)、[系统分层架构规范](../../../docs/architecture.md)、[ADR-0005](../../../docs/adr/0005-fast-path-bypass-and-decoupled-chart-workbench.md)

**Status:** ready-for-agent

- [ ] 连接器订阅实时公共流（进行中 / 闭合 K 线、`Revision`、最优盘口），实时事件经统一 `MarketEventFeed` 协议对外提供，供后续预警与扫描复用。
- [ ] 供流窗口内无成交产出 `NO_TRADES` 柱（前收盘平盘零量）；传输失联产出 `FEED_GAP` 并把 `Instrument` 置为 `STALE`；重连后回填缺口并恢复。
- [ ] 快通道 WebSocket 增量推送 K 线闭合 / 更新、最优盘口与数据源健康状态。
- [ ] 离线测试注入确定性实时样本流，覆盖修订追加、`NO_TRADES` 与 `FEED_GAP` 判定、重连补齐。
- [ ] 浏览器界面验收（均附截图）：宽度 `1440` 与 `390`、浅色与深色、简体中文下检查实时 K 线跳动、顶栏盘口与断流告警态及恢复后状态。
