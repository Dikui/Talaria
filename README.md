# Talaria

Talaria 是交易员的本地分析与研究工作台。主链路采用 **Rust 纯数据常驻服务（`talaria-data`）+ 独立看盘工作台（`workbench/`）**，负责行情接入、历史回填、`decimal128(18, s)` 冷库写入、指标、关键位、市场结构、预警与扫描（[ADR-0023](docs/adr/0023-standalone-workbench-and-rust-data-engine.md)）。Python 研究核（`core/`）通过统一标的规格与 `data_manifest.sqlite` / Arrow IPC 与 Rust 服务零误差互通；DSH 插件（`dsh/`）作为可选协同面板按需连接运行中的 `talaria-data`。

## 环境

- Rust 1.85+（Cargo workspace：`engine/`）
- Python 3.14+，[uv](https://docs.astral.sh/uv/)
- Node.js 26+，pnpm 11+
- 独立看盘前端图表：`@luxalgo/vela@0.10.17`（[归属与第三方许可](THIRD_PARTY_NOTICES.md)）

## 快速启动（独立工作台 + Rust 数据引擎）

首次构建前端与 Rust 服务：

```bash
pnpm -C workbench install
pnpm -C workbench build
cargo build --manifest-path engine/Cargo.toml --bin talaria-data
```

启动纯数据常驻服务 `talaria-data` 并托管 `/desk`：

```bash
# 默认数据目录 ~/.talaria/data，默认市场包 crypto_perp（可用 --pack okx_perp / crypto_suite 等切换）
cargo run --manifest-path engine/Cargo.toml --bin talaria-data -- up --pack crypto_perp
```

也可直接运行独立桌面端（`workbench/`，Tauri v2 自动拉起并监控 `talaria-data` sidecar）：

```bash
pnpm -C workbench tauri dev
```

### 离线确定性样本

```bash
# Bybit 离线样本
cargo run --manifest-path engine/Cargo.toml --bin talaria-data -- up \
  --pack crypto_perp --fixture-dir tests/fixtures/bybit

# OKX 永续离线样本
cargo run --manifest-path engine/Cargo.toml --bin talaria-data -- up \
  --pack okx_perp --fixture-dir tests/fixtures/okx
```

浏览器打开服务打印的 `/desk` 地址（默认绑定 `127.0.0.1` 回环地址，并自动写入会话 token 与当前数据目录路径）。`--fixture-dir` 只提供历史行情；查看实时断流与恢复状态可使用 `talaria-data replay --data-dir <隔离目录> --replay-file tests/fixtures/realtime/demo_outage.jsonl`。

## 统一数据目录与 Python SDK 互通

当未显式传入 `--data-dir` / `data_dir` 且未设置 `TALARIA_DATA_DIR` 时，**独立桌面端、Rust `talaria-data` CLI 与 Python `talaria.sdk` 均默认共用 `~/.talaria/data`**。工作台顶栏会展示当前连接的数据目录；在 Python Notebook 中可从任意工作目录直接读取同一份冷库清单与正在运行的 `talaria-data` 服务：

```python
from talaria import sdk

print("当前数据目录:", sdk.data_dir())
print("当前标的列表:", [s.instrument_id for s in sdk.instruments()])

# 无论服务端使用 crypto_perp、okx_perp、crypto_suite 还是动态添加的标的，
# SDK 都会自动按清单/服务解析到对应的交易所与交易对
bars = sdk.bars("BTC-USDT-PERP", "1m", start="2026-11-01T00:00:00Z")
print(bars.to_frame().head())
```

若需使用独立目录（例如回测实验或隔离多套数据），可同时传入 `--data-dir <dir>` 与 `sdk.bars(..., data_dir="<dir>")`，或在进程环境中统一设置 `TALARIA_DATA_DIR`。

常用支持市场包（`--pack`）：`crypto_perp`、`okx_perp`、`binance_perp`、`hyperliquid_perp`、`coinbase_spot`、`crypto_suite`、`us_equity`。

## 常用检查与测试命令

```bash
# Rust 引擎与服务测试
cargo test --manifest-path engine/Cargo.toml

# 独立工作台类型检查与测试
pnpm -C workbench typecheck
pnpm -C workbench test

# Python 研究核与契约检查（在线冒烟默认跳过）
uv sync
uv run pytest
uv run talaria contract-check
```

## 旧 Python 服务与可选 DSH 协同插件

- **旧 Python/FastAPI 服务（仅兼容过渡与回测/策略调试）**：`uv run talaria up --solo --pack crypto_perp` 启动旧版 FastAPI 服务，不再作为日常看盘主入口。
- **DSH 插件（`dsh/packages/talaria-dsh-plugin`）**：按 [ADR-0023](docs/adr/0023-standalone-workbench-and-rust-data-engine.md) 仅作为可选 Co-Pilot 协同消费方，通过 `<data-dir>/runtime/service-descriptor.json` 连接独立运行的 `talaria-data`，不再托管 `Resident Service` 启停生命周期；如仍需构建旧 DSH 前端与插件，可运行 `pnpm -C dsh install && pnpm -C dsh build`。

## 关键位离线评估

`evaluate-levels` 逐时点重放关键位引擎并对照随机价位基线：

```bash
uv run talaria evaluate-levels \
  --pack crypto_perp \
  --instrument BTC-USDT-PERP \
  --timeframe 1m \
  --from 2026-11-01T08:00:00+08:00 \
  --to 2026-11-01T18:00:00+08:00 \
  --atr-multiple 1.0 \
  --horizon-bars 20 \
  --baseline-samples 200 \
  --seed 42
```

完整架构边界与模块职责见[架构规范](docs/architecture.md)与 [ADR-0023](docs/adr/0023-standalone-workbench-and-rust-data-engine.md)。
