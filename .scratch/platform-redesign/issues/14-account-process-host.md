# 14 — 账户进程宿主：交易方案配置、插槽与回测

**What to build:** `talaria account run scheme.yaml --mode backtest` 启动一个账户进程，按方案装配插件，用 Parquet + 虚拟时钟回放历史，跑一个示例 Python 策略，结果写入账本并可复现。

**Blocked by:** 04 — Python 取数 SDK；11 — 组件接口、运行时上下文与 Python 写法。

**Source of truth:** ADR-0015；`docs/architecture.md` §6.1、§6.5；GLOSSARY 的 Account Process、Trading Scheme、Plugin Slot。

**Status:** ready-for-agent

- [ ] 交易方案配置的 Schema 与校验；插槽注册表；插件声明兼容的接口版本，不兼容时启动失败并说明原因。
- [ ] 事件循环按“数据 → 策略 → 定仓与风控 → 成交 → 记账”推进，回测与实时同一时钟语义。
- [ ] 底线 2 在宿主层强制：开仓、加仓、放宽止损只能经风控插槽；只减风险的操作可直达成交插槽。用测试插件证明绕过风控的开仓被拒绝。
- [ ] 复式账本由账户进程单写；回测写入 `runs/<run_id>/`。
- [ ] 本任务用最简的测试用定仓、风控、成交插件跑通；默认插件在 15、16 实现。
- [ ] 测试：同一方案、数据和种子下两次回测账本逐字节一致。
