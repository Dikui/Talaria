# 16 — M1 收尾：`--solo` 独立分析工作台端到端验收

**What to build:** 以交易员视角从零走通 M1 全流程并修复发现的缺陷：启动 `--solo` → 建 `Watchlist` → 看实时 K 线、指标与衍生品 → 查看并过滤关键位 → 画线 → 在画线与关键位上挂预警并收到提醒 → 收到异动卡片 → 断流与恢复 → 重启后快照可重现。

**Blocked by:** 06 — 冷库原子发布、实时切片物化与快照重启可重现；11 — 关键位第二批；12 — 关键位离线质量评估；14 — 关键位预警与关键位一键采纳；15 — 确定性异动扫描器

**Source of truth:** [需求文档](../spec.md)（用户故事 1–7）、[系统分层架构规范](../../../docs/architecture.md)（§1.3 M1、§5.3 契约门禁）

**Status:** ready-for-agent

- [x] 一条场景回归用例在注入的确定性行情上走通上述全流程，断言关键输出（快照、指标、关键位、预警触发、异动卡片）。
- [x] 架构规范 §5.3 中与 M1 相关的契约门禁（契约零漂移、十进制字符串、算子与关键位黄金向量、回环绑定与 Token 鉴权）全部通过。
- [ ] 真实 Bybit 在线冒烟（沙箱外可选）跑通一次并记录结果。
- [x] 浏览器界面验收（均附截图）：宽度 `1440` 与 `390`、浅色与深色、简体中文下检查完整工作台；覆盖空自选、无关键位、数据源断流三种状态同时出现时的页面。

## Comments

### 场景回归

新增 `tests/acceptance/test_m1_solo.py`（789 行，`pytest.mark.medium`）：

- `test_m1_solo_acceptance_flow`：同一个 `data_dir` 分三段真实 `create_app` 生命周期。
  - A 段（fixture-only，`--solo` 离线装配）：health `feed_mode=fixture`、collaboration `mode=solo`；无 Bearer 写请求 401；建“M1 验收”分组并加入 BTC；创建快照（1m+5m，cutoff 固定 `1_795_022_974_103`）；断言 `bar_counts["1m"]==905`、`["5m"]==400`、905 根、末根 close `"68685.3"`、含 `FEED_GAP`（close 为 null）与 `NO_TRADES`；EMA/RSI 最新值为十进制字符串且与 `points[-1]` 一致；衍生品 mark/funding/OI/next_funding_ts 非空；关键位 ≥20 且按 strength 降序，`STRUCTURE`+`min_strength=0.9` 过滤为非空真子集；创建 `HORIZONTAL_LINE` 画线；挂 LEVEL_EVENT TOUCH 与 OVERLAY_CROSS TOUCH ABOVE 两条预警（均 ARMED），初始触发器为空；记录 parquet sha256。
  - B 段（replay 注入，固定 `now_ms`）：加载持久化预警；重启后补发快照，断言 A 段关键位 id 仍在；按所选关键位价格生成确定性回放（24 根平静柱→触碰关键位→上穿画线→放量 spike→disconnect→reconnect→跳一分钟 K 线），断言两条预警各触发（`condition_kind`、`observed`、`snapshot_id` 非空且该快照可读）；WS 收到 `alert`；扫描器产出 `LIQUIDITY_ANOMALY`/含 `snapshot_id` 的触发器，`/api/scanner/focus` 为 `scanner-focus` 且 watchlist 出现系统组 `SCANNER_FOCUS`；health 状态覆盖 `STALE`→`RECOVERING`→`LIVE`；WS 收到 `timestamp==gap_ts` 且 `close is None` 的 `FEED_GAP` 柱。
  - C 段（无 replay 重启）：按 snapshot_id 读回 meta 与 A 完全相等、bars JSON 完全相等、`state/hot_snapshots/{id}.parquet` sha256 与 A 相同；指标最新值、关键位 id 集合一致；预警与触发器、watchlist 与 `SCANNER_FOCUS` 均持久化；无 Bearer DELETE 仍 401。
- `test_m1_solo_cli_loopback_and_token`：真实子进程 `python -m talaria up --solo`；`--host 0.0.0.0` 启动前被拒（输出含“回环”）；127.0.0.1 启动后 `/desk` 下发 `talaria_token` cookie，写端点缺 Bearer 401、带 Bearer 200。
- 测试全程离线、固定 `now_ms`、无网络/模型调用，可重复。

### 门禁结果

- `uv run talaria contract-check` → `契约生成物零漂移。`
- `uv run pytest tests/foundation/test_contracts.py tests/foundation/test_contract_check.py tests/foundation/test_dsh_tool_schema.py tests/foundation/operators/test_vectors.py tests/foundation/test_key_level_vectors.py tests/foundation/test_key_level_volume_vectors.py -q` → 47 passed（零漂移、十进制字符串、算子/关键位黄金向量、DSH 工具 Schema 子集）。
- 回环与 Token 鉴权：`test_m1_solo_cli_loopback_and_token` + 既有 `tests/workbench_api/test_app.py`。
- `uv run pytest tests/acceptance/test_m1_solo.py tests/workbench_api/test_scanner_service.py tests/workbench_api/test_scanner_api.py tests/workbench_api/test_watchlist.py -q` → 18 passed。
- `uv run pytest tests/workbench_api tests/cli -q` → 157 passed。

### 修复的缺陷

1. **`--solo` fixture 模式下 LEVEL_EVENT 预警永不触发**（阻断用户故事 6）：`AlertService` 原先只把 supervisor 实时窗口交给 `AlertEngine`，fixture-only 没有实时流时窗口为空，关键位无法解析，触发条件永远不成立。修复：`core/src/talaria/workbench_api/alert_service.py` 增加可选 `key_level_provider` 并透传；`core/src/talaria/workbench_api/app.py` 用 `app.state.level_sets` 按 `(instrument,timeframe,level_id)` 提供快照关键位，避免引擎用少量实时柱重算出不同 `level_id`。由验收用例 B 段回归。
2. **空扫描焦点会凭空创建系统分组，顶掉“空自选”首启引导**（影响用户故事 2 空态）：`ScannerService._sync_focus` 首次空焦点也 `upsert_system_group`。修复：无焦点且分组不存在时不建组（已存在的组仍保留/清空），`core/src/talaria/workbench_api/scanner_service.py`；新增 `tests/workbench_api/test_scanner_service.py::test_empty_focus_does_not_create_system_group` 回归。

### 浏览器验收

环境：本机 `pnpm -C dsh build` 构建 `talaria-desk-ui/dist`（gitignored），`talaria up --solo` + 离线 fixture + 确定性 replay；ego-browser 验收，截图在 `.scratch/tasks/ticket-16/shots/`（gitignored）：

- `01-full-light-1440x900.png`：浅色完整工作台（自选池、实时 K 线、EMA/RSI/ATR、衍生品摘要、关键位、画线、预警触发历史、异动卡片、扫描焦点）。
- `02-full-dark-1440x900.png`：同页深色。
- `03-readonly-390x844.png` / `03-readonly-390x844-dark.png`：390×844 只读视图（画线/关键位面板按窄屏隐藏，图表与自选/异动可用），浅色与深色。
- `04-empty-watchlist-1440x900.png`：空自选 + 有关键位 + 断流 STALE。
- `05-no-key-levels-1440x900.png`：关键位滑到 90 → “当前筛选没有匹配的关键位”，自选池仍有数据。
- `06-feed-stale-1440x900.png`：断流 + STALE 顶部提示、自选行 STALE 标记，预警历史仍可读。
- `07-restart-snapshot-replay-1440x900.png` 与 `07-restart-snapshot-replay-full-1440x964.png`：杀进程重启后点击“触发历史”的“定位到触发时刻”，页面重建 `快照 7a691e64 · 截止 11/19 08:20`（footer 可见），预警历史与自选池均持久化。
- `09-empty-watchlist-no-levels-stale-1440x900.png`：同一页同时出现“空自选 + 无关键位 + 断流 STALE”三态。
- `08a-alert-triggers-1440.png` / `08b-scanner-cards-1440.png`：预警触发记录与异动卡片细节（波动率扩张 ×3.5、流动性异常倍量）。

### 已知限制

- 重启后 `app.state.level_sets` 为空：已挂 LEVEL_EVENT 预警要在页面/API 至少创建一次快照填充关键位缓存后才恢复触发；重新加载 `/desk` 会自动建快照，故实际使用无感，但纯 API 场景需注意。
- 关键位 `min_strength` 为 0.9 时 ETH/1000BONK 无匹配项；这是当前关键位强度分布，不是缺陷。
- 扫描焦点分组仅在出现异动时创建；空焦点不再建空组（本次修复的有意行为）。
- 在线冒烟未跑：本机 `curl https://api.bybit.com/v5/market/instruments-info` 返回 **403**（CloudFront 地理封锁），无法完成真实 Bybit 在线冒烟；既有 `tests/packs/test_online_bybit.py`（`@pytest.mark.online`）保留为沙箱外入口，未伪造通过。
- 协同会诊（任务 17）未合入，不在本单范围，未改动相关文件。
