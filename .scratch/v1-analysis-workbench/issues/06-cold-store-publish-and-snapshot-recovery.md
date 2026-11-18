# 06 — 冷库原子发布、实时切片物化与快照重启可重现

**What to build:** 把 02 的简易落盘升级为架构规范规定的冷热一体时点存储：冷库单写者原子发布，冻结快照时实时热流未归档切片即时物化，进程重启后任意 `snapshot_id` 都能完整重现同一份 `Evidence Snapshot`。

**Blocked by:** 03 — 实时 K 线与盘口流、`NO_TRADES` / `STALE` 断流判定与顶栏健康提示

**Source of truth:** [需求文档](../spec.md)、[领域词汇表](../../../GLOSSARY.md)、[系统分层架构规范](../../../docs/architecture.md)（§2 冷热存储与 SQLite 分库）、[ADR-0006](../../../docs/adr/0006-single-position-round-per-subaccount-and-isolated-ledgers.md)

**Status:** ready-for-agent

- [ ] 冷库发布遵循“原始报文内容哈希归档 → 临时批次完整性 / 单调性 / 精度质检 → 不可变列式文件原子切换 → 冷库清单库短事务提交游标与版本”；在每一步注入中断，读取侧都看不到半成品批次。
- [ ] 冻结快照时，热流中未归入冷库的切片即时物化落盘并登记到实时快照库；列式读取只扫描快照清单显式列出的文件，禁止目录通配符。
- [ ] 冷库清单库、实时快照库、协同与感知状态库三个 SQLite 库各自单写者、开启 WAL；退出时刷盘冷库增量并执行 WAL 检查点。
- [ ] 每条记录如实写入 `Committed Time`（为 M3 的 `OBSERVED_REPLAY` 预留）。
- [ ] 离线测试覆盖中断不暴露半成品、重启后按 `snapshot_id` 逐字节一致重现、清单外文件不被读取。
- [ ] 无界面变更，不需浏览器验收。
