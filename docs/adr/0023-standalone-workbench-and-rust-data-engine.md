---
status: accepted
---

# 链路 1 作为一等核心：Rust 纯数据引擎 `talaria-data` 与独立看盘工作台

2026-11 决定：将 Talaria 从“DSH 桌面应用托管 Python 单体”重构为**以链路 1（纯数据 + 独立看盘工作台）为一等核心**的架构。

1. **一等独立产品（链路 1）**：独立看盘工作台（`workbench/`，`Tauri v2` 原生多窗口 + React 19 + `dockview-react` + `@luxalgo/vela@0.10.17`）只需启动**单个 Rust `Resident Service`（常驻服务，二进制 `talaria-data`）**即可完成行情接入、历史回填、`decimal128(18, s)` 冷库写入、指标与 `Key Level` 计算、多图联动、`Chart Overlay` 持久化、`Alert` 触发与数值扫描，**零 Python、零 DSH 运行时依赖**。`talaria-data` 同时托管同源浏览器 SPA 入口 `/desk`，桌面端与浏览器端共用同一套前端代码。
2. **Rust `Resident Service`（`engine/`）职责与接口**：
   - `talaria-contracts`：零 I/O 的 L0 契约、定点 `DecimalStr`、五重时点、`InstrumentSpec` 与六大 `Capability Traits`。
   - `talaria-operators`：基于 `rust_decimal` 的确定性算子与 `Key Level` 引擎，必须 100% 通过 `contracts/operator_vectors/` 全部 11 份黄金向量逐字符验收。
   - `talaria-store`：唯一冷库写者，写入符合 [ADR-0017](./0017-cold-store-per-instrument-decimal128.md) 物理 `INT64` 规范的 `decimal128(18, s)` Parquet（ZSTD）与 `data_manifest.sqlite`，并维护热缓冲 `HotStreamBuffer`。
   - `talaria-packs` 与 `talaria-feed`：`Market Pack` 协议与 `FeedSupervisor`（`LIVE → STALE → RECOVERING`、`NO_TRADES` / `FEED_GAP` 与 JSONL 回放时钟）；支持多市场与多交易所组合包 `crypto_suite`（合并 `okx_perp` 主日常源、`hyperliquid_perp` 链上资金费率与 `coinbase_spot` 美盘现货基准）以及 `binance_perp` 官方 `data.binance.vision`（SHA-256 校验 ZIP 归档）历史回填。
   - `talaria-server` 与 `talaria-cli`（产出 `talaria-data`）：仅绑定 `127.0.0.1` 回环地址，持有 `<data-dir>/runtime/service.lock` 排他锁并发布 `0600` 权限 `<data-dir>/runtime/service-descriptor.json`；提供 Arrow IPC 批量取数端点（`/api/bars.arrow`、`/api/ticks.arrow`、`/api/derivatives.arrow`）、兼容 JSON 接口、双语义 WebSocket（`/ws/desk` 与带 `(epoch, seq)` 重连补发的 `/ws/strategy`）以及单写工作台状态库 `state/workbench_state.sqlite`。
3. **`Resident Service` 生命周期归属**：日常桌面形态下，由独立 `Tauri v2` 桌面应用通过 sidecar（`--exit-with-parent`）启停与监控 `talaria-data`（支持探测复用已运行实例或配置关闭主窗后台驻留）；独立运行形态下，通过 `talaria-data up` 启动。不再由 DSH 桌面应用独占托管启停，也不采用 `launchd`。
4. **外围消费方边界**：
   - Python（`core/src/talaria/`）收敛为确定性研究与策略核：`foundation/`（契约与黄金向量唯一源头）、`sdk/`（直读冷库 Parquet + Arrow IPC 补尾 + 内嵌 DuckDB 查询视图）、`components/`、`account/`（`Account Process`）与 `orchestration/`。
   - DSH 插件（`dsh/packages/talaria-dsh-plugin/`）降级为可选 `Co-Pilot Analysis` 协同消费方，按 `service-descriptor.json` 连接 `talaria-data` 读取冻结 `Evidence Snapshot`、执行受控标注写入与生成 `Briefing`，不再承担看盘主入口或 `Resident Service` 生命周期管理。
5. **图表渲染核与合规**：看盘工作台通过窄接口 `ChartSurfaceAdapter` 隔离图表库，首选 `VelaChartAdapter`（`@luxalgo/vela@0.10.17` 核心与 `plugin` SDK，Apache-2.0），**严禁引入** AGPL-3.0 的 `@luxalgo/vela-pinets` 与非 React 的 `@luxalgo/vela/workspace`，并在工作台可见处保留 `"Vela"` 及 `https://velacharts.dev` 归属标注；通过 `registerRendererLayer` 原生渲染 `SYSTEM_LEVEL` / `AGENT_ANNOTATION` / `EXECUTION_BARRIER` 图层，彻底移除 `requestAnimationFrame` SVG 轮询。

## 对已有 ADR 的修订与取代

- 取代 [ADR-0020](./0020-dsh-hosts-the-whole-workbench.md)、[ADR-0021](./0021-dsh-desktop-app-as-host.md)、[ADR-0022](./0022-dsh-desktop-app-owns-service-lifecycle.md)。
- 修订 [ADR-0001](./0001-dual-plane-dsh-and-python-core.md)（演进为 Rust `Resident Service` + 独立工作台 + Python 研究/策略核 + 可选 DSH 协同）。
- 修订 [ADR-0005](./0005-fast-path-bypass-and-decoupled-chart-workbench.md)（工作台迁出 `dsh/`，渲染核升级为 `@luxalgo/vela`）。
- 修订 [ADR-0010](./0010-pure-python-indicator-core.md)（Python `foundation/operators` + `contracts/operator_vectors/` 为跨语言唯一数学权威，Rust `talaria-operators` 须 100% 通过全部黄金向量回归）。
- 后续扩展见 [ADR-0024](./0024-readonly-run-export-projection.md)（`talaria-data` 对回测 `run_export` 的只读边界）与 [ADR-0025](./0025-paper-account-projection-and-intent-queues.md)（`talaria-data` 持有模拟盘草案与干预命令队列并只读转发 `accounts/*/view.json` 投影，但不执行交易逻辑；桌面工作台托管模拟盘 `Account Process` 启停）。

## Considered Options

- **继续以 DSH 桌面应用为主宿主、Python 单体承担热路径**：Python 在高并发行情流与大区间取数上存在 GIL、事件循环抖动与跨语言序列化瓶颈（100 万行 JSON 读取耗时约 Arrow IPC 的 27.5 倍），且将独立看盘强绑定到 0.x 预览版 DSH 宿主。否决。
- **连同策略、回测与 `Account Process` 全部重写为 Rust**：策略编排、DuckDB/Notebook 研究视图与现有插件生态高度依赖 Python；Phase 0 POC 2 已实测验证 Rust 写出的 `decimal128(18, s)` Parquet 与 Arrow IPC 可零误差、高吞吐直通 Python `ColdReader` 与 `sdk/`，无需放弃 Python 策略核。否决。
- **保留 `KLineChart v10` 与 React SVG 轮询叠加层**：`KLineChart` 仍需自维护两套 `requestAnimationFrame` SVG 轮询层对齐关键位与画线；`@luxalgo/vela` 原生 `chart.drawings` 与 `registerRendererLayer` 消除轮询且支持 `FEED_GAP` 跳过与 `null` 断流语义。否决（`KLineChartAdapter` 仅保留在参考实现中作为可选回退边界）。

## Consequences

- 日常看盘不再受 Python 或 DSH 宿主生命周期、版本升级及事件循环负载影响，单 Rust 二进制即可离线或在线独立运行。
- 跨语言契约以 `core/src/talaria/foundation/` 与 `contracts/`（`schemas/` + 11 份 `operator_vectors/`）为唯一真相源；Rust `talaria-contracts` 与 `talaria-operators` 必须持续通过零漂移与逐字符黄金向量门禁。
- 现有 `core/` 热链路与 `dsh/` 在并行过渡期间保持全部测试通过，待 `talaria-data` 契约等价验收与 `sdk/` / `dsh/` 切流完成后再清理重复实现。

依据：`docs/experiments/chain1-phase0-poc.md`（仅本地）。
