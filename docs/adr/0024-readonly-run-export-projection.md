---
status: accepted
---

# `talaria-data` 对 `run_export` 回测导出投影的只读边界（`summary.json` 原子就绪标记）

2026-11 决定：扩展 [ADR-0023](./0023-standalone-workbench-and-rust-data-engine.md) 中 `talaria-data` 的职责边界，允许常驻服务以**严格只读**方式扫描并转发 `<data-dir>/runs/<run_id>/` 下的回测导出投影（`run_export` 契约文件），以 `summary.json` 作为原子就绪标记，**严禁打开 `ledger.sqlite` 或在常驻服务内执行回测**。

## 决策内容

1. **单一写者不变（`Account Process` 专属写入）**：
   - 回放与回测的权威账本 `runs/<run_id>/ledger.sqlite` 与导出投影文件（`rounds.parquet`、`equity.parquet`、可选预留 `overlays.parquet`、`summary.json`）仍由承载该次运行的 Python `Account Process`（及补导命令 `talaria runs export <run_id>`）独家写入（见 [ADR-0015](./0015-account-process-as-plugin-host.md)）。
   - `talaria-data`（`engine/crates/talaria-server`）对 `<data-dir>/runs/` 只有只读权限，不在服务进程内运行策略、撮合或回测逻辑。

2. **契约化导出文件取代直读内部 SQLite**：
   - 账本表 `execution_events` 的 `payload TEXT` 为 Python 内部序列化格式，不作为跨语言契约。跨进程边界统一使用 L0 契约 `core/src/talaria/foundation/runs.py` 导出到 `contracts/schemas/` 的 `run_export` 系列 Schema（`run_export_summary`、`run_round_record`、`run_equity_point` 等）。
   - 所有数值金额、价格、数量与偏移（`mae` / `mfe`）在 JSON 与 Parquet 列中以十进制字符串（`DecimalStr`）表示；所有汇总与曲线按 `(sub_account_id, currency)` 分组键控；首版净值口径固定为 `pnl_basis = "booked"`（与 `replay_runner.py` 的 `summarize_ledger` 一致），由工作台显式标注“已实现口径”，并配合 `rounds.parquet` 的可选 `mae` / `mfe`（按回合存续期报价极值计算，缺乏区间柱数据时允许为 `null`）观察持仓承压。
   - `overlays.parquet` 作为可选预留文件登记在 `summary.json` 的 `artifacts.overlays_path` 中，本轮不强制生成。

3. **严格原子提交顺序与 `summary.json` 就绪标记**：
   - 写者必须先写 `.tmp` 临时文件 → `fsync` 文件句柄 → 原子 `os.replace` 重命名依次产出 `rounds.parquet` 与 `equity.parquet`（及可选 `overlays.parquet`）并对目录执行 `fsync`，**最后**再以同样的 `.tmp -> fsync -> os.replace` 流程写入 `summary.json`。
   - `talaria-data` 扫描 `<data-dir>/runs/` 时只将**包含合法 `summary.json` 的目录**视为就绪运行；缺少 `summary.json` 的正在写入或半成品目录一律跳过（或标为未就绪），绝不读取其残缺 Parquet 文件，也不报错中断列表接口。
   - 相同 `run-config.json`、`run-status.json` 与 `ledger.sqlite` 重复执行导出时，不得写入墙钟时间戳等易变字段，产出须逐字节一致。

## Considered Options

- **由 `talaria-data` 直接打开各次运行的 `ledger.sqlite` 并解析 `execution_events.payload`**：会把 Python 内部 Pydantic 事件模型与 SQLite 表结构固化为隐式跨语言契约，且使服务耦合 `Account Process` 的内部账本演化与并发写入状态。否决。
- **仅写入 `summary.json`，不要求写入顺序**：在 `summary.json` 先于 Parquet 文件落盘时，工作台读取 `/api/runs/{run_id}/rounds` 或 `/equity` 会遇到文件缺失或半截 Parquet 错误。否决（采用 `summary.json` 最后落盘作为原子提交屏障）。
- **另起一套 Python HTTP 服务专供工作台读取回测结果**：违背 ADR-0023“单个常驻服务 `talaria-data` 服务独立看盘工作台、零 Python 运行时依赖”的一等架构原则。否决。

## Consequences

- 删除 `runs/<run_id>/ledger.sqlite` 后，工作台仍可通过 `talaria-data` 的 `/api/runs*` 接口完整展示回测汇总、成交标记、`MAE/MFE`、保护屏障与已实现净值曲线。
- 历史未导出的 `runs/<run_id>/` 目录可通过 `talaria runs export <run_id>` 离线补导为标准只读投影。
- 后续扩展 `overlays.parquet`（策略自定义 Python 指标/信号叠加层）或逐柱浮动净值口径时，只需在 `foundation/runs.py` 扩展契约版本与导出字段，不影响现有 `summary.json` 原子就绪机制。

依据：`docs/research/chain2-sdk-components-replay.md`（§4，仅本地）。
