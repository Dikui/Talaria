# 03 — HTTP 按时间范围取 K 线（含冷库之外的最新部分）

**What to build:** 任何使用方（SDK、DSH 工具、前端）都能通过一个 HTTP 接口按标的、周期、时间范围和 `as_of` 取 K 线；返回包括冷库尚未写入、仍在热缓冲中的最新几根，并能区分已收盘和进行中的柱。

**Blocked by:** 01 — 冷库数值格式迁移到按列精度的 decimal128。

**Source of truth:** ADR-0016、ADR-0018；`docs/architecture.md` §4.3。

**Status:** ready-for-agent

- [ ] `GET /api/bars?instrument=&timeframe=&start=&end=&as_of=&include_open=` 返回冷库与热缓冲拼接、按 `(ts_event, revision)` 去重的序列；数值为十进制字符串，每根带 `confirmed` 与 `revision`。
- [ ] 热缓冲保留 `confirmed` 与 `revision`，不再按 `ts_event` 覆盖丢失这两个字段。
- [ ] 默认只返回已收盘的柱：收盘时刻已过但确认消息未到的柱不返回；`include_open=true` 时附带进行中的柱，供图表使用。
- [ ] `as_of` 与 01 用同一实现（先截断、再取最大修订）；省略时返回当前可见全部。
- [ ] 契约从 `foundation` 导出 JSON Schema 与 TypeScript 类型，契约零漂移检查通过；OpenAPI 快照同步更新。
- [ ] 测试：跨冷热边界的范围、只在热缓冲中的范围、`as_of` 截断、收盘时刻已过但确认晚到的柱、`include_open` 两种取值。
