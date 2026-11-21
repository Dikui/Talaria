# 08 — 关键位注册表取代路由里的广播器缓存

**What to build:** 关键位预警不再依赖前端创建快照：任何来源（路由、扫描、预警）的快照冻结都把带 `cutoff_ts` 的 `KeyLevelSet` 版本写入服务内的关键位注册表；服务启动时为需要的标的预热一版；预警按事件时刻取当时的版本，图表取最新版本。

**Blocked by:** None — can start immediately.

**Source of truth:** `docs/architecture.md` §5.4。

**Status:** ready-for-agent

- [ ] `POST /api/snapshots` 路由不再承担关键位的业务写入；搜索广播器上的关键位缓存读写无残留，WS 投影行为保持不变。
- [ ] 服务启动时为有活跃关键位预警或实时订阅的 `(instrument, timeframe)` 按当前时刻从冷库算一版预热。
- [ ] 预警读取 `cutoff_ts <= 事件时刻` 的最新版本；图表读最新版本。
- [ ] 测试：没有前端连接时，由扫描触发的快照冻结后，关键位预警能按 `level_id` 触发；重启后只开关键位预警、关闭扫描，预警仍能触发；后冻结的快照里某价位已 `BROKEN`，更早时刻的事件仍按当时的 `ACTIVE` 状态判断。
- [ ] 现有关键位预警与图表投影测试保持通过。
