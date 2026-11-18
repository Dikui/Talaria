# 12 — 关键位离线质量评估

**What to build:** 维护者运行一条离线评估命令，得到各类关键位被触及后指定倍数 ATR 内出现价格反应的比例，并与同窗口随机价位基线对照，为算法与强度权重提供统计依据。

**Blocked by:** 10 — 关键位引擎第一批与 `SYSTEM_LEVEL` 图层

**Source of truth:** [需求文档](../spec.md)、[系统分层架构规范](../../../docs/architecture.md)、[ADR-0008](../../../docs/adr/0008-analysis-first-v1-and-deterministic-key-level-engine.md)

**Status:** ready-for-agent

- [x] 评估命令输入标的、周期、时间范围、ATR 倍数与随机基线样本数，按 `kind` 输出反应率、样本量与随机基线对照；随机基线使用固定种子可复现。
- [x] 评估过程逐时点调用关键位引擎（只用截止时刻前数据），测试断言评估不引入前视。
- [x] 评估自动覆盖引擎已注册的所有类别（11 完成后自然包含成交量位与清算估算）。
- [x] 输出结果的格式可以机器读取；不需浏览器验收。

## Comments

- 实现摘要（2026-11-19，分支 `ticket-12-key-level-evaluation`，基线 `5afa649`）：
  - 新增 `core/src/talaria/evaluation/key_levels.py`：`parse_time_ms`、`KeyLevelEvaluationConfig`、`evaluate_key_levels`、`load_compact_bars`、`format_table`、`dumps_report`；新增 `talaria evaluate-levels` 子命令。评估属应用层，复用 `FixtureConnector`、`compact_series`、`RawStore`，不写快照、不改 10 的引擎/契约。
  - 无前视：窗口内每个非 `FEED_GAP` 柱的 `ts_available` 作为 cutoff；每次只把 `ts_available <= cutoff` 的柱交给 `compute_key_levels`。测试用 spy 记录每次调用的最大 `ts_available` 并断言 `<= cutoff`，另构造未来会形成缺口的柱验证过去结果不变。
  - 指标复用：ATR 直接调用 `foundation.operators.compute_series` 的 `IndicatorName.ATR`（默认周期 14，Wilder 平滑），没有另写 ATR。
  - 类别：`registered_level_kinds()` 以 `KeyLevelKind` 为唯一注册表自动枚举；引擎实际发出但注册表未列出的类别追加输出。测试用 fake registry 注入 `VOLUME_PROFILE_POC` 证明新增类别无需改评估代码。
- 样本、触及与反应定义（`reaction_definition` 同时写入 JSON）：
  - 样本：同一 `level_id` 跨 cutoff 只计一次（首次发现时的对象），取评估窗口内首次触及；`sample_count` 是真实关键位样本数。
  - 触及：柱 `high/low` 包含线价；区间位按区间重叠判定。
  - 反应：触及之后严格 `horizon_bars` 根内，任一 close 离开线/区间达到 `atr_multiple * ATR(atr_period)` 即算反应，向上突破与向下反弹都算；`mean_reaction_atr` 是全部样本最大偏离的 ATR 倍数均值。
  - 跳过：首次触及后不足 `horizon_bars`、ATR 尚未预热或 horizon 内无有效 close 的样本计入 `skipped`（基线计入 `baseline_skipped`），不以 0 代替。
  - 随机基线：从同窗口非空 close 抽样 `baseline_samples` 个价位；每个 kind 用 `sha256(seed:kind)` 派生独立 `random.Random` 流；触及/反应判定与真实关键位相同，不读取引擎 `level_id`、状态或强度。
- 输出字段：输入参数 `instrument_id`、`timeframe`、`from_ts`/`to_ts`（含 `from_iso`/`to_iso`）、`atr_period`、`atr_multiple`、`horizon_bars`、`baseline_samples`、`seed`；汇总 `method_version`、`reaction_definition`、`skipped`、`baseline_skipped`、`total_sample_count`、`baseline_sample_count`、`source`；`results[]` 每项含 `sample_count`、`reaction_count`、`reaction_rate`、`mean_reaction_atr`、`skipped_count`、`baseline_sample_count`、`baseline_reaction_count`、`baseline_reaction_rate`、`baseline_mean_reaction_atr`、`baseline_skipped_count`。默认 stdout JSON，同时 stderr 输出可读表格与数据源；`--no-json` 只输出表格；错误退出码 2 并输出 `{"error": {"code", "message"}}`。
- 示例命令（真实输出摘要，非伪造）：
  `uv run talaria evaluate-levels --instrument BTC-USDT-PERP --timeframe 1m --from 2026-11-01T08:00:00+08:00 --to 2026-11-01T18:00:00+08:00 --atr-multiple 1.0 --horizon-bars 20 --baseline-samples 200 --seed 42`
  → `total_sample_count=9`、`baseline_sample_count=1805`、`skipped=3`、`baseline_skipped=595`；例如 `SWING_HIGH sample_count=3 reaction_rate=1.0 mean_reaction_atr=2.963921 baseline_sample_count=145 baseline_reaction_rate=0.917241`，`OPENING_RANGE sample_count=1 reaction_rate=1.0 mean_reaction_atr=1.108592 baseline_sample_count=157 baseline_reaction_rate=0.847134`。
- 测试结果：
  - `uv run pytest -q tests/evaluation tests/cli/test_evaluate_levels.py` → 19 passed。
  - `uv run pytest -q` → 343 passed, 1 deselected。
  - `uv run talaria contract-check` → 契约生成物零漂移。
  - 覆盖：ms/ISO/naive `Asia/Shanghai` 解析、空窗口、全类别枚举、spy 无前视、未来柱不影响过去、固定种子报告一致、基线样本数、ATR 倍数松/严阈值、ATR 预热跳过、JSON 字段、CLI 默认 JSON + 表格、上海 ISO 窗口、未知标的/周期错误码、RawStore `--data-dir` 回退、缺失数据错误码、`--no-json` 表格。
- 已知限制：
  - fixture 覆盖有限（BTCUSDT 1m 2026-11-01 00:00–15:04 UTC；5m 至 11-02 09:15；1h 至 11-09），跨窗口结论不代表长期统计。
  - 窗口尾部不足 `horizon_bars` 或 ATR 预热的随机基线会被跳过，因此 `baseline_sample_count` 可能小于 `baseline_samples`，且基线略偏向较早触及；这是诚实跳过而非 0 填充。
  - `FixtureConnector` 单次最多 1000 根，超长窗口会截断；本轮未实现分页。
  - 11 尚未合入，当前 registry 为 12 类；11 合入后成交量位/锚定 VWAP/清算估算等类别会自动出现在结果中，无需修改评估代码。
  - 只支持 crypto perp pack 的标的；`--data-dir` 仅回退读取 RawStore，不做 fixture 与 raw 的合并。
  - 未做浏览器验收（本任务只要求机器可读输出）。
- 本分支未合并、未推送；Status 保持 `ready-for-agent`。
