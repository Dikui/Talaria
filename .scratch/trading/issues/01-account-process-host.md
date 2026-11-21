# 01 — 账户进程宿主：交易方案配置、插槽与回测

**What to build:** `talaria account run scheme.yaml --mode backtest` 启动一个账户进程，按方案装配插件，用 Parquet + 虚拟时钟回放历史，跑一个示例 Python 策略，结果写入账本并可复现。插槽之间的交接契约在本单定好，默认插件在 02、03 实现。

**Blocked by:** analysis/02 — 组件接口、运行时上下文与 Python 写法。

**Source of truth:** ADR-0015；`docs/architecture.md` §6.1、§6.5；GLOSSARY 的 Account Process、Trading Scheme、Plugin Slot。

**Status:** ready-for-agent

- [ ] 交易方案配置的 Schema 与校验；插槽注册表；插件声明兼容的接口版本，不兼容时启动失败并说明原因。工具插槽只定接口，不实现插件。
- [ ] 事件循环按“数据 → 策略 → 定仓与风控 → 成交 → 记账”推进，回测与实时同一时钟语义。
- [ ] 插槽交接契约：策略输出意图（不含数量）；定仓输出 `SizingDecision`（数量与依据）；风控只批准或拒绝该数量；`MarginValuation` 只读估值契约定义接口（实现在 02）。
- [ ] 底线 2 在宿主层强制：开仓、加仓、放宽止损只能经风控插槽；只减风险的操作可直达成交插槽。用测试插件证明绕过风控的开仓被拒绝。
- [ ] 复式账本由账户进程单写；回测写入 `runs/<run_id>/`。模拟盘的子账户排他锁（`accounts/<sub_account_id>/owner.lock`）在宿主实现：两个进程声明同一子账户时第二个启动失败。
- [ ] 本任务用最简的测试用定仓、风控、成交插件跑通。
- [ ] 测试：同一方案、数据和种子下两次回测，规范化后的执行事件与复式分录一致（剔除 `run_id`、墙钟时间与数据库物理布局）；两个进程实际争用同一子账户锁。
