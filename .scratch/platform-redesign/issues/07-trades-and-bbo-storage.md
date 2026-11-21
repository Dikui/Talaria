# 07 — 逐笔成交与买一卖一落盘，历史逐笔从公开归档补齐

**What to build:** 对实时订阅的标的，服务把买一卖一和逐笔成交按 `标的/日` 写入冷库；历史逐笔可以从 Bybit 公开归档按日补齐。SDK 能用 `sdk.trades(instrument, day)` 读出。

**Blocked by:** 02 — 冷库在每根 K 线收盘后持续写入；04 — Python 取数 SDK。

**Source of truth:** `docs/architecture.md` §4.1、§4.2；`docs/research/fill-simulation-and-tick-data.md`。

**Status:** ready-for-agent

- [ ] 行情传输层增加 `publicTrade.{symbol}` 订阅；L1 盘口沿用 `orderbook.1.{symbol}`。
- [ ] 逐笔与买一卖一分区写入，数值格式与 01 一致；不存交易 ID 以外的冗余字段（是否存交易 ID 在实现中按体积实测决定并记录）。
- [ ] 历史补数命令从 `public.bybit.com` 按日下载、校验、写入；重复执行幂等。
- [ ] 落盘后记录一天的实际体积，写回 spec D3 的容量估算处。
