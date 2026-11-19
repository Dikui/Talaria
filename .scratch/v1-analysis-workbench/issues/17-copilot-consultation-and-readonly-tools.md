# 17 — 协同会诊：框选发起、只读分析工具链、`Abstain` 降级与 Token 留痕

**What to build:** 交易员在同屏工作台框选图表局部区间或点击“请求协同会诊”，智能体基于同一 `snapshot_id` 调用只读工具读取快照、指标、关键位与当前视窗，输出带证据引用的 `Setup Insight`；数据陈旧、证据冲突或超时时返回带原因的 `Abstain`。每轮 Token 用量由宿主侧幂等留痕。

**Blocked by:** 09 — DeepSeek Harness 接入预研；10 — 关键位引擎第一批与 `SYSTEM_LEVEL` 图层

**Source of truth:** [需求文档](../spec.md)、[领域词汇表](../../../GLOSSARY.md)、[系统分层架构规范](../../../docs/architecture.md)（§3.6）、[ADR-0001](../../../docs/adr/0001-dual-plane-dsh-and-python-core.md)、[ADR-0004](../../../docs/adr/0004-dual-mode-intraday-cognition-and-scanner-gate.md)、[ADR-0008](../../../docs/adr/0008-analysis-first-v1-and-deterministic-key-level-engine.md)

**Status:** ready-for-agent

- [x] 在 09 的桥上补齐 `get_indicators`、`get_key_levels`、`compute_operator`、`get_chart_view` 工具，PTC 运行时绑定同口径 Python 算子；提示词按“静态分析纪律前缀 → 加密永续 `Market Pack` 领域技能 → 当前快照增量”三段组装，不接入绕过快照的外部行情源。
- [x] 框选或点击会诊时，前端先经快通道冻结 `snapshot_id`，再拼装会诊包（`Instrument`、周期、可视范围、框选区间、有效 `HUMAN_DRAWING`、窗口内 `Catalyst Calendar` 事件（初版为空）与自然语言问题）。
- [x] `STALE` 快照、关键证据冲突、几何关系倒挂或推理超时一律输出带原因的 `Abstain`。
- [x] 宿主侧（非模型可调用工具）每轮结束按轮次标识幂等上报输入、缓存命中、输出 Token 并落库；守护进程可扫描本地会话轨迹补齐漏报。
- [x] 长推理、超时或宿主被停止时，快通道切图、画线与预警提醒不受影响。
- [x] 离线测试用模拟 / 录制桥接消息覆盖工具契约、`Abstain` 降级、Token 幂等与快通道非阻塞；真实模型会话为沙箱外可选冒烟。
- [x] 浏览器界面验收（均附截图）：宽度 `1440` 下浅色与深色、简体中文检查框选会诊交互、长篇 `Setup Insight` 与多条证据引用折叠、未发起会诊的空会话引导态、宿主忙碌 / 离线提示；宽度 `390` 下检查会诊结果可读、框选入口隐藏。

## Comments

### 实现范围

- 领域层 `core/src/talaria/foundation/collaboration/`：`contracts.py`（会诊包、Setup Insight、Abstain、Usage 契约）、`prompt.py`（三段提示词）、`validation.py`（STALE / 证据冲突 / 几何倒挂判定与宿主文本解析）、`tools.py`（4 个只读工具入参）、`usage_scan.py`（本地 DSH 轨迹补扫）。
- 服务与 API `core/src/talaria/workbench_api/collaboration_service.py`、`state_store.py`、`app.py`：`POST /api/collaboration/consult`、会话查询、宿主结果回调、`GET/POST /api/collaboration/usage`。
- DSH 桥 `dsh/packages/talaria-dsh-plugin/`：`bridge.js` 统一 `snapshot_id` 绑定与 4 个只读读取；`index.js` 注册 5 个工具与 `/talaria/consult` 端点；`guard.js` 白名单；`/talaria/consult` 用 `ctx.agents` 创建隔离会话、异步跑一轮并回调文本与 Token。
- 前端 `dsh/packages/talaria-desk-ui/src/features/collaboration/`：协同会诊面板、框选层、右栏 tab、会话流与证据折叠；`App.tsx` / `styles.css` / `features/alerts/AlertPanel.tsx` 只做共存接线。

### 工具契约

- 5 个工具：`get_snapshot` 与 4 个只读工具；除 `get_snapshot` 外 `required[0]` 均为 `snapshot_id`，桥接端每次读取前校验 `snapshot_id / instrument_id / timeframe`（`TALARIA_BRIDGE_SNAPSHOT`）。
- 指标、关键位、算子、图表视窗均转发 Python 服务，不在 JS 重算；所有价格、指标值保持十进制字符串。
- `contracts/schemas/dsh-tools/*.tool.json` 由 `uv run talaria contract-check --write` 生成，满足 DSH 子集（无 `$schema` / `$defs` / `anyOf`，对象 `additionalProperties` 布尔化）；插件目录副本与 canonical 逐字一致。

### 会诊与 Abstain

- 前端先调用 `/api/snapshots` 冻结快照（不等宿主），再 `POST /api/collaboration/consult`；会诊包包含可视范围、框选区间、有效 `HUMAN_DRAWING`、空 Catalyst 列表、证据目录与问题。
- 提示词固定三段：静态分析纪律 → 加密永续 Market Pack 技能 → 当前快照增量（含会诊包 JSON、工具摘要、证据目录与几何约束）。
- `Abstain` 原因：`STALE_SNAPSHOT`、`EVIDENCE_CONFLICT`、`GEOMETRY_INVERTED`、`MODEL_TIMEOUT`，另含 `HOST_OFFLINE`、`MODEL_INVALID_OUTPUT`；均带 `message` 与 `evidence_refs`，前端显示黄色降级卡。
- 快照超过 15 分钟、几何方向倒挂、引用未知/无证据、框选价格与交易带不相交、入场带落在可视价格区外、证据价格与交易带不相交均在服务端失败关闭。

### Token 幂等与补扫

- 宿主每轮以 `(dsh_session_id, dsh_turn_id)` 调 `POST /api/collaboration/usage`；表主键去重，重复上报返回 `duplicate: true`；`input/cached/output` 任一缺失保持 `NULL`，只有 `source="missing"` 可全空且只计 `missing_reports`，不按 0 入账。完整轮次不会被覆盖；唯一例外是原先 `missing` 的轮次允许被宿主重试或轨迹补扫补全。
- 补扫命令：`uv run talaria usage-scan --dsh-home ~/.dsh --data-dir .talaria`。读取 `session.v4.jsonl.zstd`（zstd 多 frame JSONL），按 turn 折叠 `assistant/message.data.usage`，缺字段保持 `None`；实测本机 393 个会话、1380 轮，二次运行全部判重。

### 非阻塞

- `/desk` 与快通道不依赖 DSH；宿主离线或停止时状态显示“协同宿主：离线 · 快通道正常”，会诊快速返回 `HOST_OFFLINE` Abstain，切图、画线、预警、快照冻结继续可用。
- 宿主推理超时由服务端 `overall_timeout_s` 与插件 `timeoutMs` 双重兜底，超时回调 `MODEL_TIMEOUT`。

### 验证

- `uv run pytest -q`：419 passed, 1 deselected。
- `uv run talaria contract-check`：契约生成物零漂移。
- `pnpm -C dsh typecheck`：通过（contracts 与 desk-ui）。
- `pnpm -C dsh --filter @talaria/desk-ui test`：17 文件 122 tests 通过（任务单中的 `@talaria-desk-ui` 不是真实包名，实际包名为 `@talaria/desk-ui`）。
- `pnpm -C dsh --filter @talaria/dsh-plugin test`：17 tests 通过（桥接契约、guard、5 工具注册、consult 一轮）。
- `pnpm -C dsh build`：通过。

### 截图

- `/Users/dikui/Project/Talaria/.scratch/screens/ticket-17/1440-light-collab.png`
- `/Users/dikui/Project/Talaria/.scratch/screens/ticket-17/1440-dark-collab.png`
- `/Users/dikui/Project/Talaria/.scratch/screens/ticket-17/1440-selection.png`
- `/Users/dikui/Project/Talaria/.scratch/screens/ticket-17/1440-long-insight.png`
- `/Users/dikui/Project/Talaria/.scratch/screens/ticket-17/1440-empty.png`
- `/Users/dikui/Project/Talaria/.scratch/screens/ticket-17/1440-host-offline.png`
- `/Users/dikui/Project/Talaria/.scratch/screens/ticket-17/390-result.png`（390 下框选入口隐藏，结果可读）

### 已知限制

- `Catalyst Calendar` 初版按任务单固定为空。
- 轨迹补扫目前由 `usage-scan` 命令触发，未在守护进程内定时轮询；需要常驻补扫时可挂到既有调度器。
- 真实模型会话冒烟未在本分支执行，宿主链路由离线录制与 mock host 覆盖。
- 框选依赖图表库的可见区间与像素反投影；极窄区间（< 6px）会被忽略。
