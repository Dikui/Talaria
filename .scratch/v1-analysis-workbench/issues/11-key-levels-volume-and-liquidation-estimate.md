# 11 — 关键位第二批：成交量位、锚定 VWAP 与估算清算密集区

**What to build:** `SYSTEM_LEVEL` 图层新增 `POC / VAH / VAL`、锚定 VWAP 及标准差带，以及基于持仓量变化与杠杆假设推算的清算密集区；清算密集区显式标为 `ESTIMATED`，在视觉上与观测类关键位分开。

**Blocked by:** 07 — 永续衍生品数据与衍生品指标；10 — 关键位引擎第一批与 `SYSTEM_LEVEL` 图层

**Source of truth:** [需求文档](../spec.md)、[领域词汇表](../../../GLOSSARY.md)、[系统分层架构规范](../../../docs/architecture.md)、[ADR-0008](../../../docs/adr/0008-analysis-first-v1-and-deterministic-key-level-engine.md)

**Status:** ready-for-agent

- [x] 成交量位与锚定 VWAP 按截止时刻无前视计算，接入 10 的稳定身份、强度与状态机制，补黄金向量。
- [x] 清算密集区算法的杠杆假设作为版本参数显式记录；类别标为 `ESTIMATED`，`get_key_levels` 可按观测 / 估算筛选。
- [x] 看盘页对 `ESTIMATED` 使用不同的视觉样式与图例说明。
- [x] 浏览器界面验收（均附截图）：宽度 `1440` 与 `390`、浅色与深色、简体中文下检查成交量位与清算估算区的差异化图例；覆盖持仓量数据不足时清算估算区为空的提示。

## Comments

### 实现摘要

- 后端：`foundation/key_levels/contracts.py` 增加 `POC / VAH / VAL / ANCHORED_VWAP / LIQUIDATION_ZONE` kind、`VOLUME / LIQUIDATION` category、`EvidenceClass`、`VwapAnchor`、`KeyLevel.evidence_class`、`KeyLevelSet.notes` 与成交量/VWAP/清算参数；`engine.py` 新增 `_volume_profile_levels`、`_anchored_vwap_levels`、`_liquidation_levels`，`compute_key_levels_with_notes` 统一做无前视截断、去重、排序与上限；`workbench_api/service.py` 支持 `evidence_class` 筛选并透传 OI 点与 notes；`app.py` 增加 `evidence_class / vwap_anchor / vwap_anchor_ts / value_area_ratio` 查询参数与 422 校验；WS `levels` 投影自动携带 `evidence_class` 与 `notes`。
- 契约：`contracts_io.py` 导出 `EvidenceClass`，`uv run talaria contract-check` 生成 schema 与 `generated.ts`，零漂移。
- 前端：`features/key-levels/model.ts` 增加成交量位/清算估算类别、证据类别筛选与中文标签；面板增加证据 chips、观测实线/估算虚线图例、notes 提示与“估算/OI 推算”标记；图层对估算类使用虚线（`3 3`）、半透明线与区间，并在图例区绘制 `ESTIMATED` 角标；`useKeyLevels` 透出 notes，`App` 传入面板。
- 共享样式修复：`styles.css` 中 `@media (max-width: 720px)` 缺少闭合 `}`（合并回归），导致其后的关键位面板与 DSH 插槽样式只在 ≤720px 生效；补回闭合括号后 720px 以上恢复正常，未改动画线表与关键位忽略表语义。

### 算法参数（method_version = key-levels@2）

- 成交量分布：`volume_profile_bins=24`，`value_area_ratio=0.70`，POC 取最大量 bin 中点（并列取较低价），价值区按相邻 bin 扩张至比例满足。
- 锚定 VWAP：`vwap_anchor=session`（可选 `snapshot` / `timestamp`，时间戳锚点由 `vwap_anchor_ts` 指定），`vwap_band_sigmas="1,2"`，仅用 `ts_available<=cutoff` 且有成交量的 bar，零量柱不前视推进。
- 清算估算：`leverage_tiers=5,10,25,50,100`，`liquidation_cluster_atr=0.5`，`liquidation_min_points=4`，`liquidation_min_oi_change=0.0005`，`liquidation_max_distance_atr=12.0`，`liquidation_max_levels=8`；由 OI 增量方向结合对应时段价格方向推断多/空，按 `price*(1±1/L)` 估算强平价并按 ATR 桶聚合。每个估算位在 `parameters` 记录 `source=oi_change_leverage`、`direction`、`cluster_leverages`、`oi_events`、`estimated_notional`、`cluster_width`；`KeyLevelSet.parameters` 记录全部版本参数。
- OI 不足提示写入 `KeyLevelSet.notes`（无 OI / 样本不足 / 无增量 / 缺收盘价），观测筛选时 notes 为空。

### 黄金向量

- `contracts/operator_vectors/key_levels_volume.json`（7 个用例：POC/VAH/VAL、锚定 VWAP（含零量柱）、时间戳锚点、清算估算、未来 bar 排除、OI 不足空集、无 OI 空集），`operator="key_level_volume"`。
- `tests/foundation/test_key_level_volume_vectors.py` 逐值断言 levels 投影、notes、`ts_available<=cutoff`、`formed_ts<=ts_available` 与输入顺序无关的稳定 `level_id`；`tests/foundation/test_key_level_volume.py` 覆盖算法边界，`tests/workbench_api/test_key_levels_api.py` 覆盖筛选、参数与空集提示。

### 测试结果

- `uv run pytest -q` → 338 passed, 1 deselected
- `uv run talaria contract-check` → 契约生成物零漂移
- `pnpm -C dsh typecheck` → Done（talaria-contracts / talaria-desk-ui）
- `pnpm -C dsh --filter @talaria/desk-ui test` → 99 passed（15 files）
- `pnpm -C dsh build` → 成功

### 浏览器验收

截图位于 `/Users/dikui/Project/Talaria/.scratch/tasks/ticket-11/`（未入库），均用 `read_image` 逐张确认：

- `01-1440-light-overlay-legend.png`：1440 浅色，关键位 21/26，成交量位 8、清算估算 8、观测 18、估算 8，图例与 `ESTIMATED` 角标。
- `02-1440-dark-filter-panel.png`：1440 深色，估算虚线/半透明与筛选面板。
- `03-390-light-readonly.png` / `04-390-dark-readonly.png`：390 浅/深，只读提示与底部抽屉，布局不溢出。
- `05-1440-legend-evidence.png`：证据筛选 chips 与观测/估算图例局部。
- `06-1440-oi-insufficient.png`：`1000BONK-USDT-PERP` 持仓量不足，清算估算 0，面板提示“该快照没有可用的持仓量数据，清算密集区为空。”
- `07-1440-long-labels.png`：长标签与密集标签裁剪。

### 已知限制

- 清算密集区仅按 OI 变化与杠杆假设估算，不含真实强平引擎、维持保证金分层与资金费率；数值为估算，不能与观测类关键位等价。
- 快照数据存在截止滞后，`session` 锚点在数据缺失时回退到最后一个数据 UTC 日界，避免 `formed_ts > ts_available`；实时数据无此回退。
- UI 未提供锚点/杠杆参数的编辑入口，参数通过 API 与 `KeyLevelParams` 配置并在契约/API 中只读展示。
- 本分支未合并、未推送。
