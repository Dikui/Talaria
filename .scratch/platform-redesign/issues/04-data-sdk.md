# 04 — Python 取数 SDK：一行代码取到与图表同口径的 K 线

**What to build:** 在 Notebook 或脚本里 `sdk.bars(instrument, timeframe, start, end, as_of)` 就能取到 K 线：历史按清单直读 Parquet，最新几根向服务补齐；服务没运行时只用本地数据。返回 `Decimal` 契约对象，`.to_frame()` 给 float DataFrame。

**Blocked by:** 02 — 冷库在每根 K 线收盘后持续写入；03 — HTTP 按时间范围取 K 线。

**Source of truth:** ADR-0016；`docs/architecture.md` §4.3。

**Status:** ready-for-agent

- [ ] SDK 不依赖 `workbench_api` 和 FastAPI，导入边界测试守住。
- [ ] 直读部分只读清单显式列出的文件；补尾部分按 `ts_event` 拼接去重。
- [ ] `as_of` 截断实现只有一份，供以后回测数据入口与组件 `ctx` 复用。
- [ ] 服务未运行时不报错，返回本地数据并在结果上标记未补尾。
- [ ] 测试：与 `GET /api/bars` 对同一范围的结果逐值相等；离线模式；`.to_frame()` 的列与类型。
