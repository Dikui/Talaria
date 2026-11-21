# 08 — 关键位注册表取代路由里的广播器缓存

**What to build:** 关键位预警不再依赖前端创建快照：任何来源（路由、扫描、预警）的快照冻结都把 `KeyLevelSet` 写入服务内的关键位注册表，按 `(instrument, timeframe)` 读最新；图表投影和关键位预警都从注册表读取。

**Blocked by:** None — can start immediately.

**Source of truth:** `docs/architecture.md` §5.4。

**Status:** ready-for-agent

- [ ] `POST /api/snapshots` 路由不再承担关键位的业务写入；搜索广播器上的关键位缓存读写无残留，WS 投影行为保持不变。
- [ ] 测试：没有前端连接时，由扫描触发的快照冻结后，关键位预警能拿到关键位并按 `level_id` 触发。
- [ ] 现有关键位预警与图表投影测试保持通过。
