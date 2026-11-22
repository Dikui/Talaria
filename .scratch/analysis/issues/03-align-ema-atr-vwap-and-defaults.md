# 03 — EMA、ATR、VWAP 按 TradingView 语义对齐，默认参数可由用户设置

**What to build:** 图表、预警和关键位用的 EMA、ATR、VWAP 与 TradingView 的 Pine v6 定义逐值一致（同一组 K 线、同一参数，按公开公式手算的结果）；新加 EMA/SMA 时默认周期是 9；用户可以把任一指标的参数存为自己的默认值，也能恢复出厂值；已保存的预警不受默认值变化影响。

**Blocked by:** 无。

**Source of truth:** `docs/research/tradingview-borrowing-list.md` §5、§5.1（Pine v6 参考手册条目原文已核实，标 [PINE-REF*]）；ADR-0010（单一权威 Python Decimal 实现）。算法依据只用公开文档，不接入 TradingView 私有接口取值，也不用 TV 输出做基准。

**Status:** resolved

## 现状与目标

| 指标 | Talaria 现在（`core/src/talaria/foundation/operators/core.py`） | 目标（Pine v6） |
|---|---|---|
| EMA | `_EmaEngine`：前 N−1 根为空，第 N 根以前 N 根均值作种子 | `ta.ema`：第一根以该根 close 作种子并输出，之后 `alpha*src + (1-alpha)*prev`，alpha = 2/(N+1) |
| ATR | `_AtrEngine`：跳过第一根（无前收），从第 2 根起收集 N 个 TR 求均值作种子 | `ta.atr` = `ta.rma(ta.tr(true), N)`：第一根 TR = high − low，计入种子；RMA 以前 N 个 TR 的均值作种子 |
| VWAP | `_VwapEngine`：默认 `anchor="snapshot"`，从第一根一直累计 | `ta.vwap` 默认锚点 `timeframe.change("1D")`：每个 UTC 日界重置；第一个日界之前输出空 |
| EMA/SMA 默认周期 | `DEFAULT_PERIODS` 与前端 `INDICATOR_DEFAULTS` 都是 20 | 9 |

RSI、BOLL（总体标准差）、SMA 算法已一致，不改。

## 任务

- [x] **EMA**：`_EmaEngine` 改为首值种子，第一根即有值。`reset()`（FEED_GAP 重预热）后视为新序列起点，同样从下一根首值起算。
- [x] **ATR**：`_AtrEngine` 第一根（及 `reset()` 后第一根）TR 取 high − low 并计入种子窗口；种子仍是前 N 个 TR 的简单均值，之后按 RMA 递推。
- [x] **VWAP**：默认锚点改为 `session`（UTC 日界，现有实现）；`session` 模式下，序列起点到第一个日界之前输出空（与 Pine 在首个锚点前返回 na 一致）。`snapshot` 保留为可选锚点（从序列起点累计）。改 `spec.py` 中 VWAP 的 anchor 默认值和前端 `INDICATOR_DEFAULTS.VWAP.anchor`。
- [x] **默认周期**：`spec.py` 的 `DEFAULT_PERIODS` 与 `dsh/packages/talaria-desk-ui/src/features/indicators/config.ts` 的 `INDICATOR_DEFAULTS` 中 EMA、SMA 改为 9，两处保持一一对应；更新 `config.test.ts`。
- [x] **预警参数显式化**：`AlertCondition` 当前允许 `indicator_period`/`compare_period` 为空，评估时回落到 `DEFAULT_PERIODS`（`perception/alerts/engine.py:645`、`:697`，`workbench_api/alert_service.py:350`），VWAP 锚点也没有存。改为：
  - 新建预警时由服务端把生效参数（周期；VWAP 的锚点）写入条件，存下来的条件不再依赖默认值。
  - 已存的预警做一次迁移：周期为空的按旧默认值补齐（EMA/SMA 20、RSI/ATR 14、BOLL 20），VWAP 补 `snapshot` 锚点，使迁移前后触发行为不变。
  - 评估路径不再读 `DEFAULT_PERIODS`。
- [x] **用户默认值**：服务端按指标名保存用户的默认参数覆盖（周期、BOLL 倍数、VWAP 锚点），存放沿用 `workbench_api/state_store.py` 的现有状态存储；代码里的 `DEFAULT_PERIODS` 只作出厂值。
  - 提供读取“生效默认值”（出厂值叠加用户覆盖）和写入/清除覆盖的接口，参数校验复用 `spec_from_params`。
  - 前端启动时取生效默认值初始化指标设置（替代直接用 `INDICATOR_DEFAULTS`）；指标参数面板提供“设为默认”和“恢复出厂”。
  - 用户默认值只影响新加的指标和新建的预警，不改已存预警和已打开图表上的实例参数。
- [x] **黄金向量**：按 Pine 公式重新生成 `contracts/operator_vectors/ema.json`、`atr.json`、`vwap.json`（含前导空值、FEED_GAP 重预热、增量与全量一致）。生成方式写在向量文件或测试注释里，用独立于 `core.py` 的参考实现逐值算出，不能拿新实现的输出直接回填。`sma.json` 若依赖默认周期一并更新。
- [x] **其他引用方**：检查 `foundation/indicators.py`、关键位引擎和 `tests/` 中依赖旧默认值或旧 EMA/ATR/VWAP 前导空值数量的断言，按新语义更新。关键位引擎自带的 `_atr_series`（`key_levels/engine.py:118`）是另一套口径，本单不改，在 Comments 里记一笔是否要统一。
- [x] **决策记录**：在 ADR-0010 追加一段“算法语义以 Pine v6 参考手册为准”，列出本单的三处语义与默认周期变更。

## 测试

- `tests/foundation/operators/test_vectors.py` 用新向量通过；增量 `update_series` 与全量 `compute_series` 在新语义下逐值一致。
- EMA：周期 9，第一根输出等于首根 close；用 3 到 5 根手算值断言。
- ATR：第一根 TR = high − low 计入种子，第 N 根输出等于前 N 个 TR 均值。
- VWAP：跨两个 UTC 日的序列，第一个日界前全为空，日界后从零累计；`snapshot` 锚点行为与改动前一致。
- 预警：迁移前后，周期为空的旧预警在同一组 K 线上的触发结果一致；新建预警存下的条件带显式周期和锚点。
- 用户默认值：设置覆盖后新加指标与新建预警使用覆盖值；清除后回到出厂值；非法参数被拒绝。
- 前端 `config.test.ts` 与相关组件测试通过。

## 不做

- 指标模板（一组指标及参数的命名保存）：留给借鉴清单 §6 第 2 批。
- MACD、Stochastic 等新指标：另开任务单。内置 MACD 默认参数尚未核实（借鉴清单 §7）。
- 关键位引擎 ATR 口径统一。

## Comments

### 实现记录（analysis-03，2026-11-22）

**后端语义**
- EMA 改为首值种子：第一根输出 close，之后 `alpha = 2/(N+1)` 递推；`FEED_GAP` 后下一根重新作种子。
- ATR 第一根 TR 取 `high - low` 并计入前 N 个 TR 的简单均值种子，之后 RMA；`FEED_GAP` 后同样重新收集种子。
- VWAP 默认锚点改为 `session`；首个 UTC 日界前输出空，日界处清零并从当前 bar 累计；`snapshot` 行为保持原样。
- `DEFAULT_PERIODS` 的 EMA、SMA 改为 9，RSI/ATR/BOLL/VWAP 等其余默认值不变。
- 黄金向量用 `tests/foundation/operators/pine_reference.py`（独立于 `core.py` 的 Decimal 参考实现）重新生成 `contracts/operator_vectors/{ema,atr,vwap}.json`；向量包含前导空值、跨 UTC 日界和 `FEED_GAP` 后重预热，`post_gap_warmup` 支持 VWAP 依赖日界的数据相关重预热。

**预警参数显式化**
- `AlertCondition` 新增 `indicator_anchor` / `compare_anchor`，仅 VWAP 允许设置。
- 新建/修改预警时服务端按生效默认值补齐周期与 VWAP 锚点，把显式值写进条件；评估层只读条件中的参数，不再回退 `DEFAULT_PERIODS`。
- `AlertService.start()` 执行一次性迁移（meta 标记 `alerts.explicit_params.v1`）：旧默认 EMA/SMA 20、RSI/ATR/BOLL 14/14/20、VWAP 1 且锚点 `snapshot`；显式值不覆盖。迁移幂等。
- 契约重新生成：`contracts/schemas/*`、`dsh/packages/talaria-contracts/src/generated.ts`；OpenAPI 快照 `tests/workbench_api/openapi_before_router_split.json` 已更新。

**用户默认值**
- 新增 `workbench_api/indicator_defaults.py`，把用户覆盖存入状态库 meta 键 `indicator_defaults.v1`；出厂值仍来自 `DEFAULT_PERIODS` / `IndicatorParams.defaults_for`，BOLL 出厂倍数显式为 2。
- 新增 `GET /api/indicator-defaults`、`PUT/DELETE /api/indicator-defaults/{name}`；参数校验复用 `spec_from_params`，非法参数返回 422。指标 GET 接口按生效默认值计算，非默认参数仍走 `POST /api/operators/compute`。
- 前端启动时读取生效默认值初始化指标设置（读取失败回退出厂值）；参数面板为每个指标提供“设为默认”“恢复出厂”。保存默认值只写服务端，不改变当前图表实例参数；“恢复出厂”会用出厂参数重置当前面板。
- 预警表单不再强制发送旧周期：周期留空/0 表示由服务端按生效默认值补齐；VWAP 主/对比指标增加锚点选择。

**验证**
- `.venv/bin/pytest tests/foundation/operators tests/perception/test_alerts_engine.py tests/workbench_api/test_alert_service.py tests/workbench_api/test_alerts_api.py tests/workbench_api/test_indicators_api.py tests/workbench_api/test_indicator_defaults_api.py tests/workbench_api/test_openapi.py -q` → 94 passed。其中迁移测试同时放入“周期为空”和“显式旧周期 20”的两条预警，验证迁移后在相同 K 线上同一根触发。
- `cd dsh && pnpm --filter @talaria/desk-ui typecheck` → 通过；`pnpm --filter @talaria/desk-ui test` → 22 个测试文件、159 个测试通过。
- `.venv/bin/talaria contract-check --write` → exit 0，重复执行无额外改动。

**关键位 ATR 口径**
- `key_levels/engine.py:118` 的 `_atr_series` 未改：它是“TR 的滚动简单均值”，第一根 TR 也含 `high - low`，但与算子核的 RMA/种子语义不同。是否统一留待关键位专项任务，本单不做。

**风险与遗留**
- VWAP `session` 在 `FEED_GAP` 后会等到下一个 UTC 日界才恢复输出；这是“重置后从新锚点起算”的直接结果，重预热长度依赖数据，已用 `post_gap_warmup` 覆盖。
- 迁移只处理周期为空的历史预警；若历史条件中存有其他非法值，仍按原有校验处理。
- 前端默认值读取为一次性启动流程，服务端不可用时静默使用出厂值；未做浏览器手工验收，界面交互仅由 typecheck/单测覆盖。
- 手工检查建议：打开指标设置确认 EMA/SMA 显示 9、VWAP 默认“UTC 日界”；保存 EMA 周期后新建 EMA 指标沿用新值；“恢复出厂”回到 9；新建 VWAP 预警在预警列表中回读锚点；跨日 K 线前段 VWAP 为空、日界后重新累计。


**合并记录（2026-11-22）**

- 并入 `integration/platform-redesign-batch-1`（merge commit `4301e96`）；`runtime.py` 冲突按“保留 platform/01 的功能开关可空装配，同时注入 indicator_defaults”解决。
- 合并测试发现 `dsh/packages/talaria-dsh-plugin/create_alert.tool.json` 未随新增的 `indicator_anchor`/`compare_anchor` 更新；已从 `contracts/schemas/dsh-tools/create_alert.tool.json` 同步，`tests/cli/test_dsh_tool_schema.py` 15 passed。
- 合并后复跑 `pytest tests/perception tests/foundation tests/workbench_api tests/cli tests/evaluation tests/feed -q --deselect tests/cli/test_talaria_desk_profile.py::test_real_dump_config_matches_permission_snapshot` → 595 passed, 1 deselected。
- 前端 `pnpm --filter @talaria/desk-ui typecheck` 通过；`pnpm --filter @talaria/desk-ui test` → 22 files / 159 tests passed。

### 验收返工（2026-11-22）

| # | 确认与修复 | 回归用例及结果 |
|---|---|---|
| 1 | 确认。1m 窗口截掉 UTC 日界，普通收盘漏报，较旧修订还可能使 deque 插入报错。保留一个完整 UTC 日及前一柱，实盘新订阅按需分页读取闭合历史。实时修订和显式断流优先，历史与实时不连续时等待有效锚点。 | `tests/perception/test_alerts_engine.py::test_session_vwap_alert_keeps_utc_anchor_beyond_600_bars`、`tests/workbench_api/test_alert_service.py::test_session_vwap_alert_backfills_utc_boundary_on_a_fresh_start`、`tests/workbench_api/test_runtime.py::test_alert_session_history_pages_to_one_bar_before_utc_midnight` 均通过。 |
| 2 | 确认。旧 EMA/ATR 在短历史过早出值。v2 迁移为主、对比指标分别固定种子版本，旧规则使用原始播种与 Decimal 运算顺序，新规则使用 Pine。 | `tests/workbench_api/test_alert_service.py::test_migrated_ema_and_atr_alerts_match_baseline_source_bars` 和 `::test_alert_migration_after_v1_preserves_seed_provenance_and_boll_default` 通过。独立的 5cd280c 参考实现逐柱推进时钟，触发源柱和值均一致。 |
| 3 | 确认。DSH 跨端口 PUT 预检原返回 400。允许 PUT 后返回 200。 | `tests/workbench_api/test_indicator_defaults_api.py::test_dsh_can_preflight_saving_indicator_default` 通过。 |
| 4 | 确认。并发更新可覆盖另一指标的值。把读取、修改和写入放在同一次 StateWriterQueue 操作中。 | `tests/workbench_api/test_indicator_defaults_api.py::test_concurrent_defaults_save_and_clear_preserve_other_indicators` 通过，覆盖并发保存及并发清除与保存。 |
| 5 | 确认。工作台读取生效默认值后创建初始实例，启用新指标时选定参数，每次计算显式传参。“恢复出厂”只更新新实例使用的默认值。 | `features/indicators/config.test.ts` 的 `waits for defaults before requesting chart instances and pins explicit parameters`、`uses saved or cleared defaults only when enabling a new instance` 通过。 |
| 6 | 确认。预警表单未选锚点时省略该字段。服务端分别储存主、对比 BOLL 倍数，并为历史缺值补 2。 | `features/alerts/model.test.ts` 的 `lets the server supply both untouched VWAP anchors`、`tests/workbench_api/test_alert_service.py::test_new_alert_pins_boll_multiplier_and_vwap_anchor_from_user_defaults` 通过。 |
| 7 | 确认。Session VWAP 关键位复用算子核的 UTC 日界与断流语义。 | `tests/foundation/test_key_level_volume.py::test_session_vwap_levels_wait_for_utc_boundary_and_rewarm_after_gap`、`tests/workbench_api/test_key_levels_api.py::test_vwap_anchor_is_configurable` 通过。后者分别使用同日 1m 和跨日 5m 样本。 |
| 8 | 确认。指标 GET 在读取默认值后，将同步快照读取和指标计算交给线程池。 | `tests/workbench_api/test_indicators_api.py::test_indicator_get_computes_outside_the_event_loop` 通过。 |
| 9 | 确认。周期使用严格整数，非 BOLL 指标携带有效值的 `std_dev` 或非 VWAP 指标携带锚点时返回 422。 | `tests/workbench_api/test_indicator_defaults_api.py::test_defaults_reject_non_integer_period_and_irrelevant_params` 四组输入均通过。 |
| 10 | 确认。把 NET_ODDS 接入工具栏设置和默认值映射。 | `features/indicators/config.test.ts` 的 `exposes NET_ODDS and initializes new instances from its saved period` 通过。 |
| 11 | 确认。EMA 和 ATR 的递推改为 Pine 参考公式的乘加顺序。独立参考实现重生成 EMA、ATR 向量，VWAP 重生成后逐字节一致。 | `tests/foundation/operators/test_core.py::test_ema_uses_pine_decimal_operation_order_with_repeating_alpha`、`::test_atr_uses_pine_rma_decimal_operation_order_with_repeating_alpha` 及算子黄金向量均通过。 |

**实际验证**

在本工作树根目录执行，`DSH_HOME` 指向新建的临时目录。

```sh
talaria_test_dsh=$(mktemp -d /tmp/talaria-analysis-03-dsh.XXXXXX)
DSH_HOME="$talaria_test_dsh" PYTHONPATH="$PWD/core/src" /Users/dikui/Project/Talaria/.venv/bin/pytest tests/perception tests/foundation tests/workbench_api tests/cli tests/evaluation tests/feed -q -p no:cacheprovider
```

- 全范围结果为 `6 failed, 611 passed, 1 skipped, 1 warning, 1 error in 22.52s`。6 个失败和 1 个 fixture 错误均由沙箱拒绝绑定回环套接字导致。
- 同一命令用 `--deselect` 排除上述 7 个用例后，结果为 `611 passed, 1 skipped, 7 deselected, 1 warning in 20.96s`。排除项为 `test_host_dispatch_ignores_ambient_http_proxy`、`test_up_dual_keeps_fast_path_when_dsh_fails`、`test_up_dual_keeps_fast_path_when_dsh_binary_missing`、`test_host_starts_probes_and_stops_with_redacted_log`、`test_sigterm_shuts_down_gracefully`、`test_usage_scan_fails_closed_when_service_endpoint_is_unreachable`、`test_usage_scan_writes_through_running_service`。临时 DSH 目录下无已安装插件，实际 profile 快照测试跳过。
- `cd dsh && pnpm --filter @talaria/desk-ui typecheck && pnpm --filter @talaria/desk-ui test` 通过，22 个文件、163 项测试。
- `PYTHONPATH="$PWD/core/src" /Users/dikui/Project/Talaria/.venv/bin/python -m tests.foundation.operators.pine_reference --write` 已重生成向量。使用同一 `PYTHONPATH` 执行 `/Users/dikui/Project/Talaria/.venv/bin/talaria contract-check --write`，并再次运行 `contract-check` 确认零漂移。同步 DSH 工具副本和 OpenAPI 快照。
- `git diff --check` 通过。

**假设与未验证项**

- v1 迁移已把缺省周期写成显式值。v2 用 v1 标记的时间戳和规则创建时点判断原规则是否早于 Pine 切换。数据库无法还原已迁移规则最初是否显式填写了周期，也无法区分后续修改与触发造成的更新时间变化。
- 实盘补历史由合成分页连接器和预警回放覆盖。真实交易所和 DSH 浏览器交互未手工验证。
- 首次 `git add` 被沙箱拒绝，报错为 `Unable to create '/Users/dikui/Project/Talaria/.git/worktrees/analysis-03/index.lock': Operation not permitted`。返工文件均留在 `fix/analysis-03` 工作树，且无新提交哈希。基准为 `af85480`。

**返工合并记录（2026-11-22）**

- 三个返工分支 `fix/data-access-01`、`fix/platform-01`、`fix/analysis-03` 依次并入 main（最后一个合并提交 `33f1bd4`）；`snapshot_builder.py` 冲突按“保留修订选版 + 行情关闭时不回填并报 FEATURE_DISABLED”合并。
- 合并后复跑：全量 `pytest` → 691 passed, 1 deselected；`talaria contract-check` 零漂移；desk-ui typecheck 通过，163 tests passed。
