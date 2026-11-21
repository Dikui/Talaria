---
status: accepted
---

# 在完整执行计划层设置 Execution Seam，模拟优先并暂缓实盘柜台对接

现阶段系统聚焦高保真模拟验证（历史回放模拟 + 实时行情影子模拟 `Shadow Run`），暂不接入真实资金柜台下单或引入重型第三方执行引擎（如 NautilusTrader）。在软件框架中，将可替换的 `Execution Seam` 设在托管完整交易生命周期的 `ExecutionEngine` 深接口上：新建或扩大仓位严格以 `Approved Execution Plan` 为唯一入口（放宽止损距离亦须先经组合风控层签发审批凭证），同时在接口上原生提供只减不增风险的止损收紧、紧急撤单/减平仓及 `Trading State` 切换能力，统一输出标准 `ExecutionEvent`。当前仅在 Seam 后实现纯 Python 高保真 `SimExecutionAdapter`，实盘柜台容错留给未来平级替换的 `LiveExecutionAdapter`。

## Considered Options

- **把 Seam 设在底层原子订单接口（`submit_order / cancel_order / modify_order`）**：接口看似简单，实则是浅模块——入场超时撤单、部分成交同步修改止损单数量、移动止损盯盘全部泄漏到上层业务模块，日后切换实盘柜台时仍需联动修改上层逻辑。
- **第一阶段直接集成锁定版本的 NautilusTrader 作为模拟与执行底座**：虽然其原生具备 Rust 永续保证金与多交易所连接器，但在尚未验证智能体日内策略净收益前，过早引入重型编译依赖、版本迁移差异与 `LGPL-3.0` 约束会显著拖慢快速实验迭代。

## Consequences

- **诚实仿真红线不可裁剪**：为避免日内与永续模拟产生虚高收益，`SimExecutionAdapter` 至少必须强制实现以下四项物理约束（完整六条法则见架构规范 §6.4）：① 注入真实的 `Decision Latency (Δt)` 时间偏移；② 区分 Maker 穿价成交与 Taker 盘口滑点/费率；③ K 线内同时触及止盈止损时按保守先止损结算；④ 照常执行永续整点 `Funding Settlement` 与 `Mark Price` 模拟强平。
- **未来实盘零侵入升级**：当模拟验证具备扣除全成本后的稳定正期望时，发送前持久化 Outbox、`client_order_id` 去重、网络超时 `UNKNOWN` 查询对账及原生柜台/Nautilus 适配全部封装在新建的 `LiveExecutionAdapter` 内部，上游数据、扫描、DSH 认知、组合风控与账本评测代码无需重构。
