# 08 — 衍生品进入冷库与实时订阅

**What to build:** 标记价、资金费率、持仓量像 K 线一样进入冷库（`标的/类别/月`），实时来自 `tickers.{symbol}`，历史经 REST 回填；资金费率在结算点生成结算事件。SDK、`ctx.derivatives(kind)` 和策略订阅都能取到，没有服务时回测也能用。

**Blocked by:** 02 — 冷库持续写入：收盘柱与历史回填。

**Source of truth:** `docs/architecture.md` §4.2、§4.3；ADR-0017（费率类 scale 固定 8）、ADR-0018。

**Status:** ready-for-agent

- [ ] 冷库写入器支持衍生品分区与清单，数值格式与 01 一致；修订与 `as_of` 规则与 K 线相同。
- [ ] 实时：从现有 `tickers.{symbol}` 读数写入；历史：REST 回填，重复执行幂等。现有快照里的衍生品文件改为从冷库取。
- [ ] 资金费率结算事件（结算时刻、费率、可见时刻）可按范围读取，供成交插件生成 `FundingSettled`。
- [ ] `sdk.derivatives(instrument, kind, start, end, as_of)`；策略订阅可订阅衍生品更新。
- [ ] 测试：仓库样本资金费率 `0.00005` 无损往返；停服务后回测能读到标记价与结算事件；`as_of` 截断。
