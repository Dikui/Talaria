---
status: accepted
---

# 统一市场结构引擎（Market Structure Engine）：几何原语固定、检测器可插拔

2026-11 决定：将 [ADR-0008](./0008-analysis-first-v1-and-deterministic-key-level-engine.md) 确立的确定性关键位引擎演进为**统一市场结构引擎（`Market Structure Engine`）**，通过“固定 4 种时间-价格几何原语 + L0 纯函数检测器注册表 + 统一生命周期/去重/共振后处理管线”，同时支撑水平关键位、斜向自动趋势线、未来平行通道及更多市场结构算法的零穿层横向扩展。

## 决策内容

1. **统一域模型与对外命名，不保留长期双套兼容别名**：
   - 在代码契约、HTTP/WS API、状态存储与 DSH 工具层面，统一采用 `StructureFeature`、`StructureSet`、`StructureDetector`、`StructureRegistry`、`/api/structures`、`SYSTEM_STRUCTURE` 与只读工具 `get_structures`。
   - 领域业务术语（见 [GLOSSARY.md](../../GLOSSARY.md)）以 `Market Structure` 为上位概念，以 `Key Level` 与 `Trendline` 指代其水平与斜向子类（映射至 `family` 字段）。
   - 不在核心主线长期维护 `key_levels` 与 `structures` 并行的双套 API 或双套注册表；Phase 1–3 期间仅保留存量代码保证编译与测试绿灯，完成切流后在收尾任务单中一次性清理旧 `key_levels` 冗余模块。

2. **契约层一次定齐 4 种正交几何原语与统一 `bounds_at(ts)` 求值**：
   - 所有结构特征归约为带判别标签 `kind` 的 `StructureGeometry` 联合体：`HorizontalLineGeometry`、`HorizontalZoneGeometry`、`SlopeLineGeometry`（支持 `LINEAR` / `LOG` 对数坐标与 `extend_right`）及 `ChannelGeometry`。
   - 所有上层能力（`ACTIVE / BROKEN / FLIPPED / EXPIRED` 状态机、预警规则穿越求值、工作台 `@luxalgo/vela` 只读渲染与命中测试、一键采纳至 `HUMAN_DRAWING`、DSH 证据引用与离线 PIT 评估）**只面向 `StructureGeometry` 与 `bounds_at(ts)` 编程**，不感知具体检测算法。
   - 契约与渲染层在首版即完整覆盖全部 4 种原语；算法检测器首版落地水平位（17 类迁移 + FVG + EQH/EQL + 多源共振带 `CONFLUENCE_ZONE`）与斜向趋势线（`pivot_trendline@1` + 滚动凸包最优包络 `convex_envelope@1`），`Channel` 检测器在后续作为独立 L0 插件零穿层插入。

3. **L0 `DetectorRegistry` 纯函数插拔与 Python 评估先行、达标下沉 Rust**：
   - 遵循 [ADR-0013](./0013-foundation-holds-only-pure-contracts-and-computation.md)，`DetectionContext`（严格按 `ts_available <= cutoff_ts` 截断并预计算 ATR、多尺度确认 Pivot 与缺口 `island_id`）、`StructureDetector` 插件表与 `PostProcessor` 后处理管线全部作为无 I/O 纯函数留在 L0。
   - 遵循 [ADR-0010](./0010-pure-python-indicator-core.md) 与 [ADR-0023](./0023-standalone-workbench-and-rust-data-engine.md)：Python L0 为唯一数学权威，核心内置检测器导出逐检测器黄金向量并在 Rust `talaria-operators` 逐字节对齐；新算法先在 Python L0 实现并通过 `evaluation/structures.py` 逐柱 PIT 实证评估，确认优于随机基线后再下沉进 Rust。

4. **重写聚焦新主线，冻结旧过渡代码**：
   - 本次架构重写覆盖 `core/foundation`、`core/components`、`core/evaluation`、`engine/`（`talaria-contracts` / `talaria-operators` / `talaria-server`）、独立看盘工作台 `workbench/` 及 DSH 插件 `dsh/packages/talaria-dsh-plugin`。
   - 按 [ADR-0023](./0023-standalone-workbench-and-rust-data-engine.md) 已降级退场的旧 `dsh/packages/talaria-desk-ui` 冻结不再同步开发；Python `workbench_api` 仅保持过渡期兼容测试通过，随链路 1 全面切流一并下线。

## Considered Options

- **仅在 `KeyLevel` 旁并行复制一套 `Trendline` 硬编码纵线**：短期改造成本略低，但每次新增几何类结构（如平行通道、斐波那契、形态）都要重复穿透 L0、黄金向量、Rust 服务端、前端图层与评估器 6 层，长期扩展成本呈线性放大。否决。
- **长期并存 `KeyLevel` 与 `Trendline` 两套 API/表结构**：虽无需迁移存量，但会导致跨层类型分裂、预警规则重复、智能体工具与面板碎片化。否决。
- **在原有 `key_levels` 之上做彻底结构重构并同步改写旧 `dsh/packages/talaria-desk-ui`**：引入大量即将退役前端的无效返工，拖慢主线交付。否决。

## Consequences

- 新增任意市场结构检测算法只需在 L0 注册一个 `StructureDetector` 纯函数并附带黄金向量/评估用例，服务缓存、图表渲染、一键采纳、预警触发、智能体引用与离线 PIT 评估均自动适配。
- 趋势线默认显式绑定 `analysis_window`（起始至截止 K 线区间）与 `PriceScaleMode`（`LINEAR` / `LOG`），严格消除未确认摆动点与重绘（`backpaint`）引入的前视偏差。
- 迁出旧 `foundation/key_levels` 与 `talaria-operators/src/key_levels.rs` 采用分阶段任务单推进：Phase 1–2 新旧契约短期并存以保持全仓测试绿灯，最终切换与旧代码清理在收尾任务单中一次性完成。

依据：`docs/research/auto-trendlines-and-key-levels.md`（仅本地）；`.scratch/market-structure-engine/spec.md`（仅本地）。
