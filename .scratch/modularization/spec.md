# modularization：按业务能力拆成可独立运行的模块

Status: wontfix（2026-11-20 被 ../platform-redesign/ 取代：需求改为按产品功能拆分）

## 目标

每个业务模块都能脱离 Web 层和其他模块单独构造、单独运行、单独测试：

- **独立构造**：只凭自己的配置和一组端口（Protocol）就能 `build`，不依赖 `Runtime`、FastAPI 或其他模块的具体类。
- **独立运行**：`talaria run <module>` 用 fixture 或 JSONL 回放驱动，把输出写成 JSONL，供分析和回归对比。
- **独立测试**：模块测试用假端口，不起 `TestClient`；Web 层测试只测路由契约。

`talaria up` 的行为、HTTP/WS 契约、状态库文件和前端都保持不变。

## 现状要点（2026-11-20，基于代码核对）

- 包级依赖已是单向无环：`foundation` ← `packs/feed/replay/perception` ← `evaluation` ← `workbench_api` ← `cli`。L0 边界由 `tests/foundation/test_import_boundaries.py` 守住。
- 业务服务全部在 `workbench_api/` 下（约 7.3k 行）：`alert_service`、`scanner_service`、`briefing_service`、`collaboration_service`、`drawing_service`、`annotation_service`、`watchlist`、`key_level_feedback`、`usage_service`、`service.py`（`WorkbenchService`）。
- 仓储已按领域拆开（architecture-cleanup 03），每个服务只访问自己的表，**唯一例外**：`scanner_service` 直接写 `store.watchlist` 的系统分组 `SCANNER_FOCUS_GROUP_ID`。
- 服务之间的直接调用很少，且都是只读：
  - `DrawingService`：`get_drawing` / `get_chart_view` / `create_drawing`，被 alerts、annotations、collaboration、key_level_feedback 使用。
  - `WorkbenchService`：`get_snapshot` / `get_bars` / `get_key_levels`，被 annotations、collaboration 使用。
  - `UsageService.record`，被 briefings 使用。
- `runtime.py` 是唯一装配点，模块间连接写成闭包：`freeze_snapshot`、`bar_history`、`alert_key_level`、`rebalance_watchlist`。
- **隐藏耦合**：关键位预警读的是 `DeskBroadcast.levels_for()`，而这个缓存只在路由 `POST /api/snapshots`（`routes/analysis.py:45`）里写入。没有前端创建快照时，预警拿不到关键位；业务数据流穿过了 HTTP 路由和 WS 广播器。
- 共同修改记录（93 个提交）显示，改动以“契约（foundation）+ 服务 + 路由 + 前端 feature”的纵向切片为主，说明按业务能力纵向拆分与实际改动方式一致。

## 目标结构

```
core/src/talaria/
  foundation/      L0 纯契约与纯计算（不变）
  packs/ feed/ replay/        行情源与回放（不变）
  perception/      存储与引擎库：冷热库、快照构建、AlertEngine、ScannerEngine（不变，作为库被模块使用）
  modules/
    market_data/   行情管道：supervisor + 热缓冲 + 快照构建/查询 + K 线/衍生品读取
    analysis/      指标、算子、关键位计算 + KeyLevelRegistry + 关键位采纳反馈
    alerting/      预警规则、触发、冷却（包 AlertEngine）
    scanning/      扫描与触发路由（包 ScannerEngine）
    desk/          画线、Agent 标注、自选分组与实时订阅配额
    copilot/       扫描简报、会诊、token 用量
  workbench_api/   只剩 FastAPI 路由、WS 广播、组合根
  cli/             talaria up / talaria run <module> / 其余命令
```

每个模块目录统一为：

```
modules/<name>/
  __init__.py      只导出 build_<name>、Module 类和公开类型
  ports.py         本模块需要的外部能力（Protocol），由消费方定义
  service.py       业务逻辑（从 workbench_api/*_service.py 迁入）
  repository.py    本模块的表（从 state_repositories/ 迁入）
  build.py         build_<name>(settings, ports) -> <Name>Module
  run.py           独立运行入口：读 fixture/回放，输出 JSONL
```

### 模块清单与端口

| 模块 | 迁入的现有代码 | 依赖的端口（输入） | 对外提供（输出） |
|---|---|---|---|
| market_data | `service.py` 中快照/K 线/衍生品部分、`runtime.py` 中 supervisor/热缓冲/快照构建装配 | MarketPack、可选 FeedTransport | `EventSource`（`add_listener`）、`SnapshotReader`、`SnapshotFreezer` |
| analysis | `service.py` 中 `get_indicators`/`compute_operator`/`get_key_levels`、`key_level_feedback` | `SnapshotReader`、`DrawingReader` | `KeyLevelRegistry`（冻结快照时写入，按 `(instrument, timeframe)` 读最新）、`on_levels` 发布回调 |
| alerting | `alert_service`、alerts 仓储 | `EventSource`、`BarHistory`、`KeyLevelSource`、`DrawingReader`、`SnapshotFreezer` | `on_trigger` 回调、`active_instrument_ids` |
| scanning | `scanner_service`、`TriggerRoutingPolicy` 装配 | `EventSource`、`SnapshotFreezer`、`FocusSink`、`CoPilotConsultChannel` | `on_trigger` 回调 |
| desk | `drawing_service`、`annotation_service`、`watchlist`、`subscriptions`、对应仓储 | `SnapshotReader`、`KeyLevelSource`、可选 `RealtimeSubscriptions`、`AlertedInstruments` | `DrawingReader`、`FocusSink`（实现扫描焦点分组）、自选服务 |
| copilot | `briefing_service`、`collaboration_service`、`usage_service`、`usage_scan`、对应仓储 | `SnapshotReader`、`KeyLevelSource`、`DrawingReader`、`host_url` | `CoPilotConsultChannel`（`offer`）、`on_briefing` 回调 |

端口约定：

- **端口由消费方在自己的 `ports.py` 里定义**，只写它真正调用的方法（例如 alerting 的 `DrawingReader` 只有 `get_drawing`）。提供方不需要 import 消费方，组合根负责把实现传进去；结构化类型（Protocol）自然匹配。
- 发布类输出一律是回调（`Callable[[T], None]`），沿用现有 `publish=` / `on_trigger=` 写法，WS 广播只是其中一个订阅者。
- 状态库写入继续用一个 `StateWriterQueue`（ADR-0012 不变），但每个模块只拿到**限定到自身仓储的写入口**，不能再通过 `store.<其他表>` 跨写。

### 依赖规则（加导入边界测试）

1. `modules.X` 只能 import `foundation`、`perception`、`packs`、`feed`、`replay` 和自身。
2. `modules.X` 不能 import `modules.Y`、`workbench_api`、`cli`、`fastapi`。
3. `workbench_api` 只能通过各模块 `__init__` 导出的名字使用模块。
4. 规则写进 `tests/modules/test_import_boundaries.py`，做法照搬 L0 的边界测试。

### 组合根

`workbench_api/runtime.py` 改为按顺序调用各模块的 `build_*`，把一个模块的输出接到另一个模块的端口上，不再写业务闭包：

```
market_data → analysis（SnapshotReader）
analysis.KeyLevelRegistry → alerting（KeyLevelSource）、desk、copilot
desk.DrawingReader → alerting、analysis、copilot
desk.FocusSink → scanning
copilot.offer → scanning（CoPilotConsultChannel）
各模块 on_* 回调 → DeskBroadcast（WS）
```

`talaria up` = 全部模块 + Web 层；`talaria run <module>` = 该模块 + 它需要的最少上游（通常只有 market_data 的回放模式）+ 用假实现补齐其余端口。

### 独立运行入口

| 命令 | 输入 | 输出 |
|---|---|---|
| `talaria run market-data --replay-events x.jsonl` | 回放事件 | 冻结的快照目录 + 健康状态 JSONL |
| `talaria run analysis --fixture-dir d --cutoff T` | fixture 快照 | KeyLevelSet / 指标 JSON |
| `talaria run alerting --replay-events x.jsonl --rules r.json` | 回放 + 预警规则 | 触发记录 JSONL |
| `talaria run scanning --replay-events x.jsonl [--config c.json]` | 回放 | 扫描触发 JSONL（co-pilot 通道默认关闭） |
| `talaria run copilot --trigger t.json` | 单个扫描触发 | 简报请求/结果（宿主离线时输出降级结果） |

输出 JSONL 每行一个契约对象（现有 Pydantic 模型的 `model_dump(mode="json")`），可以直接 diff，也可以存为回归基线。desk 模块是纯状态管理，只提供库级构造，不单独做 `run`。

## 迁移步骤（每一步 `talaria up` 行为不变、全量测试保持绿）

1. **KeyLevelRegistry 取代广播器缓存**：在 analysis 层新增注册表，快照冻结（不管来自路由、扫描还是预警）时写入；`DeskBroadcast` 和 alerting 都从注册表读取。路由不再承担业务数据流。先修这一步，因为它同时是潜在缺陷。
2. **扫描焦点分组改走 `FocusSink`**：`scanner_service` 不再 import watchlist 仓储常量、不再写 `store.watchlist`。
3. **建 `modules/` 骨架和导入边界测试**；先迁 alerting 作为样板（端口、build、run、假端口测试齐全）。
4. 拆 `WorkbenchService` 为 market_data + analysis。
5. 迁 desk、scanning、copilot。
6. `runtime.py` 收敛为组合根；加 `talaria run <module>`。
7. 测试调整：服务测试改为模块级假端口测试，`tests/workbench_api/` 只保留路由契约和 OpenAPI 快照；更新 `docs/architecture.md`，新增 ADR 记录“按业务能力纵向拆模块、端口由消费方定义”。

每步都是一张可独立验收的任务单，1、2 可以并行，3 之后串行（都会改 `runtime.py`）。

## 不做的事

- 不拆前端：`talaria-desk-ui/src/features/` 已按功能划分，与本方案的模块边界一一对应，暂不改。
- 不拆进程：所有模块仍在一个进程里，单机部署不变；独立运行指的是可单独构造和驱动，不是微服务。
- 不按模块拆 SQLite 文件（见待定 2）。
- 不动 M3 规划的 L2–L5；新层按同一模块约定加入即可。

## 待确认的决策

1. **路由放哪里**：方案是路由留在 `workbench_api/routes/`，模块不依赖 FastAPI。备选是路由随模块走（纵向切片更彻底，但模块会依赖 Web 框架，独立运行时要带上 FastAPI）。
2. **状态库**：方案是保持一个 `state.sqlite` 和一个写线程，只按模块收窄写入口。备选是每个模块一个 SQLite 文件、各自写线程，隔离更彻底，但要改 ADR-0012 并做数据迁移。
3. **模块粒度**：desk 把画线、标注、自选合在一起（它们共用图表上下文，修改常一起发生）；copilot 把简报、会诊、用量合在一起。也可以再细拆。
4. **perception 的引擎是否搬进模块**：方案是 `AlertEngine`、`ScannerEngine` 留在 perception 作为库，模块只包服务层，改动面最小。
