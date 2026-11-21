# 15 — 默认定仓与风控插件：按止损距离定仓与七步风控

**What to build:** 交易方案选 `sizing: r_unit` 与 `risk: seven_step` 后，计划按止损距离定仓，经过七步风控、单一仓位回合不变量、风险预留与回撤熔断；被拒的计划留下原因记录。

**Blocked by:** 14 — 账户进程宿主。

**Source of truth:** `docs/architecture.md` §6.3；ADR-0015（单一仓位回合与 `R` 定仓为默认插件规则）；GLOSSARY 的 Risk Unit、Position Round、Risk Reservation、Trading State。

**Status:** ready-for-agent

- [ ] `evaluate_candidate`、`approve_barrier_widening`、`preview_sizing` 三个接口。
- [ ] 七步风控每一步有独立测试；反向开仓返回 `OPPOSITE_POSITION_OPEN`；加仓沿用回合屏障且总风险不超 `max_single_r_pct`。
- [ ] 同一子账户串行审批与风险预留：两笔并发计划不会基于同一旧状态同时放行。
- [ ] 回撤熔断按阈值切 `REDUCING` / `HALTED`。
- [ ] `preview_sizing` 单次耗时 < 5 ms（记录实测值）。
