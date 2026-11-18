# 10 — 关键位引擎第一批（结构位 / 时段参考位 / 缺口与整数关口）与 `SYSTEM_LEVEL` 图层

**What to build:** 交易员在看盘页打开只读 `SYSTEM_LEVEL` 图层，看到系统按截止时刻确定性识别的结构位、`Trading Session` 参考位、未回补缺口与整数关口，可按类别与强度过滤，对单条关键位标记“忽略”作为质量反馈。关键位带跨快照稳定的 `level_id`，随闭合 K 线增量更新状态。

**Blocked by:** 04 — 单一实现技术指标算子核与图表主副图指标（强度与容差使用 ATR）

**Source of truth:** [需求文档](../spec.md)、[领域词汇表](../../../GLOSSARY.md)、[系统分层架构规范](../../../docs/architecture.md)、[ADR-0002](../../../docs/adr/0002-capability-traits-and-market-packs.md)、[ADR-0008](../../../docs/adr/0008-analysis-first-v1-and-deterministic-key-level-engine.md)

**Status:** resolved

- [x] 无前视纯函数产出：① 结构位（摆动高低点与多次触及聚成的区间；摆动枢轴须经后续确认柱数闭合后才产出，`Available Time` 记为确认柱收盘时刻）；② 时段参考位（前日高 / 低 / 收、周与月开盘价、开盘区间，以及由 `SessionTrait` 提供的加密亚洲 / 伦敦 / 纽约分段高低点，核心引擎不硬编码时区）；③ 未回补跳空缺口与整数关口。借鉴参考项目算法时遵守 01 的许可证结论，洁净室实现。
- [x] 每个关键位具备稳定 `level_id`、单价或区间、周期、强度评分与构成明细（触及次数、最近触及时刻、成交量权重）、状态（`ACTIVE | BROKEN | FLIPPED`）与算法版本参数；黄金向量覆盖确认延迟无前视与状态转换。引擎接口可扩展新类别（11 加入成交量位与清算估算）。
- [x] 快通道开放 `get_key_levels`（按周期、类别、强度筛选排序），WebSocket 单向投影 `SYSTEM_LEVEL` 图层，不混入画线表。
- [x] 看盘页按类别与强度阈值过滤显隐；“忽略”操作持久化到协同与感知状态库。“采纳为人工画线”由 14 实现。
- [x] 浏览器界面验收（均附截图）：宽度 `1440` 与 `390`、浅色与深色、简体中文下检查过滤面板与关键位图例；覆盖长标签防重叠、高强度过滤下无匹配的空列表提示。`390` 下只读显示。

## Comments

- 实现摘要（2026-11-19，分支 `ticket-10-key-levels`，基线 `8980d35`）：
  - 新包 `core/src/talaria/foundation/key_levels/`：`contracts.py` 定义 `KeyLevel` / `KeyLevelSet` / 忽略反馈与 `KeyLevelParams`，`engine.py` 为无 I/O、无前视的纯函数 `compute_key_levels(...)`；引擎只依赖 foundation，不硬编码交易所/时区。
  - 结构位由 ATR 分形枢轴识别，必须等后续确认柱闭合后才产出，`ts_available` 取确认柱收盘时刻；多次触及按 ATR 容差聚合为区间。稳定 `level_id = kl_ + sha256(instrument|kind|timeframe|anchor_ts|normalized_price|tag)[:16]`，不随输入数组顺序变化。
  - 时段参考位：前日高/低/收、周/月开盘、开盘区间与 `SessionTrait` 分段高低（crypto pack 显式提供亚洲/伦敦/纽约时段，核心只接收 `SessionProfile`）。缺口只保留未被后续 K 线完全回补的跳空；整数关口按价格量级推导步长。
  - 状态机：形成后首根收盘突破区间，末根收盘回到区间内为 `ACTIVE`、沿突破方向为 `BROKEN`、反向为 `FLIPPED`。
  - 快照只读 API `GET /api/snapshots/{snapshot_id}/key-levels`（PIT 截断、按类别/最低强度过滤与排序）；忽略 `GET/PUT /api/key-levels/ignored` 经 `StateWriterQueue` 串行写入 `collaboration_state.sqlite` 的 `key_level_ignores` 表。WS 新增单向 `levels` 投影（`SYSTEM_LEVEL`），不改动 `bar`/`book`/`health` 事件，也不写入 `drawings` 表。
  - 前端新增 `features/key-levels/`：只读主图 SVG 图层、类别/强度过滤面板、四色图例、忽略操作、空列表提示、长标签贴边/省略/避让；`390` 下为只读视图，过滤面板可查看不可修改，不挤压 04 指标条、05 画线工具栏与 08 左栏。
- 算法参数（`method_version = key-levels@1`）：`swing_span=2`、`confirm_bars=2`、`atr_period=14`、`pivot_atr_min=0.6`、`cluster_atr=0.4`、`touch_atr=0.2`、`gap_atr_min=0.35`、`opening_range_minutes=30`、`round_levels_per_side=3`、`round_window_atr=25`、`max_levels=60`；强度上限 0.95，排序 `strength desc`。参数与版本随 `KeyLevelSet` 返回。
- 黄金向量：`contracts/operator_vectors/key_levels.json` 6 个案例，覆盖确认柱前不可见（空集）、确认后出现、`ACTIVE→BROKEN→FLIPPED` 共享 `level_id`、区间聚合锚点稳定；`tests/foundation/test_key_level_vectors.py` 逐值断言且检查所有 `ts_available <= cutoff_ts`。08 的算子向量加载器增加 operator+params 过滤，避免把更高层引擎向量误当单算子向量。
- 测试结果：`uv run pytest -q` → 284 passed, 1 deselected；`uv run talaria contract-check` → 零漂移；`pnpm -C dsh typecheck` → 通过；`pnpm -C dsh --filter @talaria/desk-ui test` → 15 个文件 / 94 passed；`pnpm -C dsh build` → 成功（已有 500kB chunk 警告）。
- 浏览器验收（ego-browser，目录 `/Users/dikui/Project/Talaria/.agents/worktrees/ticket-10-key-levels/.scratch/tasks/ticket-10/`，均已 `read_image` 确认非空白）：
  - `01-1440-light-overlay-legend.png`（1440x960）：浅色、`SYSTEM_LEVEL` 图层、面板 17/18、四类图例与标签。
  - `02-1440-dark-filter-panel.png`（1440x960）：深色、强度 0.55、14/18。
  - `03-1440-high-strength-empty.png`（1440x960）：仅缺口 + 0.90 阈值、0/18，显示空列表提示。
  - `04-1440-long-label-edge.png`（1440x900）：面板关闭，长区间标签右贴边翻转/省略，不压画线提示。
  - `05-390-readonly.png`（390x935）：390 只读、底部抽屉过滤面板与四色图例、类别/强度控件 disabled、标签隐藏仅保留线。
  - `06-1440-ignored-after-refresh.png`（1440x900）：忽略 `kl_99b374562aebc3fc` 后 16/17，刷新后仍隐藏并提示忽略计数。
- 已知限制：本批结构位只用单周期闭合 K 线的分形与 ATR 聚类，未纳入成交量分布/清算估算（11）；分段时段高低仅对 `timeframe <= 1h` 生效；`OBSERVED_REPLAY` 仍为 501；忽略只记录质量反馈，不提供“撤销忽略”以外的采纳行为（14 实现采纳，面板行尾已留扩展点注释）；浏览器验收使用 fixture 守护进程，非实时行情。


### Lead 合并验收（2026-11-19）

- 合并 `bafaf7f Merge branch 'ticket-10-key-levels'`；解决 `workbench_api/service.py`、`feed/messages.ts`、`feed/reducer.ts`、`feed/messages.test.ts` 与 07 的冲突，保留衍生品 `derivatives` 事件与关键位 `levels` 事件、两边 reducer 状态与解析测试。
- 合并后全量：`uv run pytest -q` → 324 passed, 1 deselected；`uv run talaria contract-check` 零漂移；`pnpm -C dsh typecheck` 通过；desk-ui 97 passed；plugin 12 passed；`pnpm -C dsh build` 成功。
- 目视复核 01/05：1440 浅色下 `SYSTEM_LEVEL` 虚线、四类图例、17/18 计数、强度滑杆与忽略按钮正常；390 为只读底部抽屉，类别/强度控件 disabled，仅保留线。接受 390 下抽屉覆盖部分图表、1440 下右侧过滤面板覆盖价格轴区域的布局取舍，后续 14/16 可再优化。
- 已知限制沿用子代理记录：结构位仅单周期 ATR 分形、分段时段仅 `timeframe <= 1h`、`SYSTEM_LEVEL` 随快照/订阅下发而非逐根闭合增量重算、忽略暂无“撤销”控件；真实 Bybit 联网未验证。
- 截图已复制到 `.scratch/tasks/ticket-10/`（6 张）；worktree 与分支在关闭后删除。
