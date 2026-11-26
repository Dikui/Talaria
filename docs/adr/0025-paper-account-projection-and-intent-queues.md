---
status: accepted
---

# 模拟盘账户只读投影、交易意图队列与工作台进程托管边界

2026-11 决定：扩展 [ADR-0023](./0023-standalone-workbench-and-rust-data-engine.md) 中 `talaria-data`（`Resident Service`）与独立 `Tauri v2` 工作台的职责边界，打通“图表下单 / 改单 → 模拟盘 `Account Process` → 账户投影与回执回到图表”的链路 3 闭环，同时保持 [ADR-0015](./0015-account-process-as-plugin-host.md) 与 [ADR-0024](./0024-readonly-run-export-projection.md) 确立的单写者与进程隔离不变量。

## 决策内容

1. **常驻服务持有“交易意图队列”，但不执行任何交易逻辑（D1）**：
   - `talaria-data` 在单写工作台状态库 `state/workbench_state.sqlite`（专用写通道，[ADR-0012](./0012-state-store-dedicated-writer-thread-and-read-only-connections.md)）中持久化两类出站队列：
     - **草案队列（`Staged Plan Draft`）**：承接新建、版本化编辑、确认（校验 `(version, content_hash)`）、丢弃（`DISCARDED`，仅限未领取）、按 `valid_until` 惰性过期（`EXPIRED`，过期不出站）、出站领取（`next-draft`）与回执（`draft-receipt`），以及重冻结证据（`draft-evidence`，附当前未收盘 `1m` 柱）。未确认的草案没有进入账户进程的通道（[ADR-0015](./0015-account-process-as-plugin-host.md)）。
     - **人工干预命令队列（`PaperCommand`）**：承接持仓干预与交易状态切换命令的入队、按子账户 `seq` 串行出站领取与回执（`PaperCommandReceipt`）。
   - `talaria-data` 不运行策略、定仓、风控或模拟撮合；模拟盘 `Account Process` 按秒级心跳（`PaperPulse`）主动向 `talaria-data` 轮询领取已确认草案与干预命令，并在执行后回传回执。

2. **账户进程原子写出只读投影 `accounts/<sub_account_id>/view.json`，服务绝不打开账本（D2）**：
   - 模拟盘 `Account Process` 仍持有 `accounts/<sub_account_id>/owner.lock` 排他锁，是 `accounts/<sub_account_id>/ledger.sqlite` 的唯一写者（[ADR-0006](./0006-single-position-round-per-subaccount-and-isolated-ledgers.md)、[ADR-0015](./0015-account-process-as-plugin-host.md)）。
   - 沿用 [ADR-0024](./0024-readonly-run-export-projection.md) 的原子提交约定（`.tmp → fsync → replace`）：账户进程每次账本事务提交后原子写出 `accounts/<sub_account_id>/view.json`，并在 `PaperPulse` 周期内刷新 `process.heartbeat_ts`；进程优雅退出时写出停止态投影（`process.status = STOPPED`）。投影遵循跨语言 L0 契约 `contracts/schemas/paper_account_view.schema.json`，包含 `account_state`、活跃回合（含 `phase`、入场带、`ExitBarrierSpec`、`Mark Price` 与浮动盈亏）、最近成交、`feed_status`、`process` 与单调递增 `ledger_seq`。
   - `talaria-data` 严格以只读方式扫描并监视 `accounts/*/view.json`，通过 `/api/paper/*` 与 `/ws/desk`（`paper_account` 事件）转发给工作台；**严禁打开 `ledger.sqlite`，亦不得试探 `owner.lock`**（避免短时抢锁导致账户进程启动或守卫失败）。
   - 服务仅根据投影中的 `process.heartbeat_ts` 与 `process.status` 判定存活：进程标为已停止或 `heartbeat_ts` 超过 5 秒未刷新时，一律判定为 `OFFLINE`。不把 Python 内部账本表结构或 `execution_events.payload` 暴露为跨语言契约。

3. **干预命令直达/过风控分流、按 `command_id` 幂等与按 `based_on_ledger_seq` 判过期（D3）**：
   - 人工干预命令不走 `Staged Plan Draft` 的版本化审阅与确认流程（由界面二次确认或拖拽松手确认后直接入队）。
   - 账户进程内严格遵循 [ADR-0015](./0015-account-process-as-plugin-host.md) 底线 2 进行路由：
     - **只减风险操作**（`TIGHTEN_STOP`、`MOVE_TO_BREAKEVEN`、`CANCEL_ENTRY`、`CLOSE_ALL`、`REDUCE_RATIO`，以及将 `TradingState` 切向 `REDUCING` / `HALTED`）直达成交插槽（`Execution Seam`），成交插槽校验风险单调不增；
     - **放宽风险操作**（`WIDEN_STOP`、放宽类 `MODIFY_TAKE_PROFIT`，以及将 `TradingState` 恢复至 `ACTIVE`）须先经风控插槽审批，通过后再执行。
   - 每条命令携带唯一 `command_id` 与前置视图序号 `based_on_ledger_seq`：账户进程将命令处理结果记入账本以保证按 `command_id` 幂等（崩溃重启后跳过已处理命令）；当回合已终结或屏障已被更新导致前提过期时回执 `COMMAND_STALE`，参数无变化时回执 `NOOP`，风控拒单时回执对应拒单码。

4. **独立工作台（Tauri v2）托管模拟盘账户进程启停（D5）**：
   - 桌面形态下，由独立 `Tauri v2` 工作台沿用托管 `talaria-data` 的原生进程监管机制，按用户所选 `Trading Scheme` 启停与监控本地模拟盘 `Account Process`（默认关闭桌面工作台时同时停止由其拉起的账户进程，支持在设置中改为后台保留）。
   - 浏览器 `/desk` 形态不拉起进程，仅根据投影展示账户进程在线/离线状态及手动 CLI 启动命令。
   - **`talaria-data` 常驻服务严禁拉起或宿主化 `Account Process`**，确保策略或模拟盘异常不会拖垮行情与看盘服务。

5. **跨语言定仓预演边界（参见 [ADR-0010](./0010-pure-python-indicator-core.md) 修订，D4）**：
   - 图表与表单拖动止损线时的低延迟定仓预演（`SizingPreviewRequest` / `SizingPreviewResponse`，目标 `< 5ms`）由 `talaria-data` 基于账户投影中的状态与参数在 Rust 侧无副作用计算，并以 Python 端生成的定仓黄金向量作为唯一数学权威逐字符校验；预演只用于展示，实际下单的权威数量仍由 `Account Process` 接收已确认草案时计算。

## Considered Options

- **由 `talaria-data` 直接打开 `accounts/<id>/ledger.sqlite` 读取账本并试探 `owner.lock` 判活**：会把 Python 内部 Pydantic 账本事件序列化格式固化为隐式跨语言契约，且试探排他锁会干扰账户进程的单写守卫导致启动失败。否决（沿用 [ADR-0024](./0024-readonly-run-export-projection.md) 的原子 JSON 投影与心跳字段）。
- **由 `talaria-data` 拉起并管理 `Account Process` 子进程**：把行情与数据服务和交易执行进程耦合在一起，违背 [ADR-0015](./0015-account-process-as-plugin-host.md)“策略与交易逻辑不进常驻服务”与 [ADR-0023](./0023-standalone-workbench-and-rust-data-engine.md) 独立看盘的隔离原则。否决（桌面形态改由 Tauri 壳统一监管，浏览器形态只读状态）。
- **所有人工干预复用 `Staged Plan Draft` 确认流程**：减风险与紧急平仓操作需要最短路径直达成交插槽，复用版本化草案审阅会拖慢紧急干预且与 [ADR-0015](./0015-account-process-as-plugin-host.md) 底线 2 不符。否决。
- **把 `accounts/<id>/view.json` 并入 [ADR-0024](./0024-readonly-run-export-projection.md) 修订而不单独建 ADR**：模拟盘投影具有持续心跳、在线/离线判定、实时 `/ws/desk` 推送以及配套的双向交易意图队列，语义明显不同于“回测结束后一次性只读导出”。因此独立成篇。

## Consequences

- `talaria-data` 保持零 Python 运行时依赖：在没有启动任何 `Account Process` 时仍可独立看盘，只在收到草案/命令或发现 `accounts/*/view.json` 时提供模拟盘接口；离线账户的最后一次投影仍可读并明确标注离线。
- 删除或锁定 `accounts/<id>/ledger.sqlite` 后，工作台仍可通过 `view.json` 正常展示账户状态，从结构上证明服务不读账本。
- Python `foundation/paper.py`、`contracts/schemas/`、Rust `talaria-contracts` 与 TypeScript `@talaria/contracts` 保持单一真相源与零漂移；新增投影字段全部可缺省，兼容旧版 `PaperAccountView` 与 `PaperRound` JSON。

依据：`.scratch/chain3-paper-trading/spec.md`（D1–D7，仅本地）；`docs/research/chain3-paper-account-chart-trading.md`（仅本地）。
