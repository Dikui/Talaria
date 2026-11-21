# 06 — 策略实时订阅：不静默丢弃，断线按时间戳补齐

**What to build:** 策略和脚本用 `async for ev in sdk.subscribe(instrument, timeframe)` 订阅实时 K 线。服务端队列满时断开连接而不是丢弃；SDK 重连后按最后收到的 `ts_event` 经范围接口补齐，再继续实时推送。图表页面的“满了丢最旧”保持不变。

**Blocked by:** 04 — Python 取数 SDK。

**Source of truth:** ADR-0016；`docs/architecture.md` §4.1。

**Status:** ready-for-agent

- [ ] 新的策略订阅 WS 端点与图表 `WS /ws/desk` 分开，语义不同。
- [ ] 测试：人为让客户端变慢，验证服务断开、SDK 补齐后序列无缺口无重复；服务重启后 SDK 自动重连补齐。
- [ ] 记录一次实时延迟（事件到回调 p50/p99），与 D2 实测数据对比。
