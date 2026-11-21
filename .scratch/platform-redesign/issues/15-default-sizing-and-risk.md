# 15 — 默认定仓与风控插件：按止损距离定仓与七步风控

**What to build:** 交易方案选 `sizing: r_unit` 与 `risk: seven_step` 后，定仓插件按止损距离给出数量与依据，风控插件核对并经过七步风控、单一仓位回合不变量、风险预留与回撤熔断；被拒的计划留下原因记录。

**Blocked by:** 14 — 账户进程宿主。

**Source of truth:** `docs/architecture.md` §6.3；ADR-0015（单一仓位回合与 `R` 定仓为默认插件规则，插槽交接）；GLOSSARY 的 Risk Unit、Position Round、Risk Reservation、Trading State。

**Status:** ready-for-agent

- [ ] 定仓插件 `size` 与 `preview_sizing`；风控插件 `evaluate_candidate(candidate, sizing, ...)` 与 `approve_barrier_widening`。风控只批准或拒绝定仓给出的数量，不重算。
- [ ] `MarginValuation` 的 `crypto_perp` 实现：逐仓预估强平价、全仓账户级压力测试，按标记价估值；16 共用。
- [ ] 七步风控每一步有独立测试；反向开仓返回 `OPPOSITE_POSITION_OPEN`；加仓沿用回合屏障且总风险不超 `max_single_r_pct`。
- [ ] 同一子账户串行审批与风险预留：两笔并发计划不会基于同一旧状态同时放行。
- [ ] 回撤熔断按阈值切 `REDUCING` / `HALTED`。
- [ ] 测试：只把定仓换成一个按名义金额定仓的测试插件，风控仍按其数量核对 `max_single_r_pct` 并能拒绝超限计划。
- [ ] `preview_sizing` 单次耗时 < 5 ms（记录实测值）。
