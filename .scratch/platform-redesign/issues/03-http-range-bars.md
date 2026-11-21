# 03 — HTTP 按时间范围取 K 线（含冷库之外的最新部分）

**What to build:** 任何使用方（SDK、DSH 工具、前端）都能通过一个 HTTP 接口按标的、周期、时间范围和 `as_of` 取 K 线；返回包括冷库尚未写入、仍在热缓冲中的最新几根。

**Blocked by:** 01 — 冷库数值格式迁移到按标的精度的 decimal128。

**Source of truth:** ADR-0016；`docs/architecture.md` §4.3。

**Status:** ready-for-agent

- [ ] `GET /api/bars?instrument=&timeframe=&start=&end=&as_of=` 返回冷库与热缓冲拼接、按 `ts_event` 去重的序列；数值为十进制字符串。
- [ ] `as_of` 按 `ts_available <= as_of` 截断；省略时返回当前可见全部。
- [ ] 契约从 `foundation` 导出 JSON Schema 与 TypeScript 类型，契约零漂移检查通过；OpenAPI 快照同步更新。
- [ ] 测试：跨冷热边界的范围、只在热缓冲中的范围、`as_of` 截断、未收盘柱的处理。
