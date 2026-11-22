# 01 — JSON 规则与预警升级

**What to build:** 预警条件可以写成组合规则（如“收盘上穿 EMA20 且 RSI < 70 且资金费率为负”），以带版本号的 JSON 表达式树保存，在服务内求值；已有的单条件预警自动迁移为等价规则，触发行为不变。

**Blocked by:** analysis/02 — 组件接口、运行时上下文与 Python 写法。

**Source of truth:** ADR-0019；`docs/architecture.md` §5.2、§5.3。

**Status:** ready-for-agent

- [ ] JSON 规则 Schema（带版本号），只能引用已注册算子；显式声明求值时机：收盘求值或事件驱动（ADR-0019）；导出 JSON Schema 与 TypeScript 类型。
- [ ] 事件驱动规则只能拿当前事件的值比较常量、画线、关键位或已收盘的组件输出；引用进行中柱算出的指标时校验失败。
- [ ] 规则编译到组件接口，在服务内和账户进程内用同一求值器。
- [ ] 现有 `AlertCondition` 迁移为规则，原来盘中触发的条件（画线与关键位触碰、买一卖一与资金费率阈值）迁移为事件驱动规则；旧形式在迁移后移除，搜索旧条件类型无残留；预警冷却、画线触碰、关键位事件行为保持。
- [ ] 扫描可以使用同一规则形式。
- [ ] 测试：迁移前后对同一回放的触发记录一致，包括一段“盘中穿越后收盘前回落”的回放，触发时刻与冻结证据不变；非法规则（未注册算子、版本不兼容）被拒绝。


## Comments

### 实现记录

- 新版 `Rule` 使用 `version: "1"`、`timing: "bar_close" | "event"` 和 `expression`。节点支持比较、上穿与下穿、且或非、画线和关键位事件。指标名称、版本、参数和通道在接收规则时校验。事件比较只读当前事件的数值和已收盘的指标输出，盘中触碰由画线和关键位节点处理。表达式限制为最多 64 个节点、5 层。
- `talaria.components.compile_rule` 返回通用信号组件。常驻服务、扫描器和账户进程调用方可以使用相同的求值器，通过 `Ctx` 提供确认柱与按时点可见的数据，并由宿主提供画线和关键位。实时预警继续使用原订阅周期、冷却状态机、证据冻结与通知路径。
- 预警服务启动时在 SQLite 保存点内迁移历史规则。阈值、画线触碰、关键位触及迁为事件驱动，指标和收盘突破、回踩迁为收盘规则。旧 EMA/ATR 沿用 `legacy_ema` / `legacy_atr`，旧 VWAP 锚点、BOLL 倍数以及已固定的参数保持原值。历史触发记录和状态字段原样保留。API 使用原 `condition` 字段传递新版规则树。
- `POST /api/scanner/scan` 支持可选的收盘 `rule`，用于手动扫描；默认扫描继续使用内置条件。可复用的规则 Schema 在 `contracts/schemas/rule.schema.json`，另有对应 TypeScript、OpenAPI 和 DSH 草案工具契约。现有工作台快捷表单生成新版规则，组合规则可显示并可修改预警设置。图表规则编辑器归 02。未发现任务单与 ADR-0019 或架构 §5.2、§5.3 的冲突。
- 回放用相同的盘中穿越、收盘回落事件验证迁移。迁移后的触发发生在第二个盘中事件，冻结快照引用、观测价、阈值及消息均与迁移前已记录的结果一致。EMA/ATR 回归同时核对历史触发序列。

#### 验证

最终 Python 回归 200 通过、1 失败。唯一失败的 `test_m1_solo_cli_loopback_and_token` 在 `sock.bind(("127.0.0.1", 0))` 收到 `PermissionError: [Errno 1]`。最终命令如下。

```sh
PYTHONPATH=$PWD/core/src /Users/dikui/Project/Talaria/.venv/bin/python -m pytest \
  tests/components tests/foundation/test_import_boundaries.py tests/feed/test_supervisor.py \
  tests/perception/test_alerts_engine.py tests/perception/test_alerts_level_event.py tests/perception/test_scanner.py \
  tests/workbench_api/test_alert_service.py tests/workbench_api/test_alerts_api.py \
  tests/workbench_api/test_annotations_api.py tests/workbench_api/test_scanner_api.py \
  tests/workbench_api/test_key_level_registry.py tests/workbench_api/test_openapi.py \
  tests/workbench_api/state_repositories/test_legacy_database.py \
  tests/workbench_api/test_strategy_subscription.py::test_lone_5m_indicator_alert_receives_5m_closes \
  tests/workbench_api/test_strategy_subscription.py::test_stale_chart_channel_does_not_suspend_a_healthy_alert_period \
  tests/workbench_api/test_strategy_subscription.py::test_ordinary_alert_releases_quota_during_cooldown \
  tests/workbench_api/test_strategy_subscription.py::test_evicted_alert_reestablishes_baseline_after_quota_returns \
  tests/workbench_api/test_strategy_subscription.py::test_alert_expiring_during_a_feed_event_releases_its_quota \
  tests/workbench_api/test_strategy_subscription.py::test_evicted_alert_receives_quota_notice \
  tests/workbench_api/test_strategy_subscription.py::test_new_desk_connection_sees_current_alert_quota_denials_and_recovery \
  tests/acceptance/test_m1_solo.py tests/cli/test_cli.py::test_contract_check_has_zero_drift \
  -q -p no:cacheprovider --tb=short
```

开发迭代使用相同的 Python 与 `PYTHONPATH`。除标记 `--tb=short` 的执行外，参数为 `-q -p no:cacheprovider`。

| pytest 参数中的路径 | 结果 |
| --- | --- |
| `tests/components/test_rules.py tests/perception/test_alerts_engine.py::test_intrabar_touch_replay_freezes_before_price_returns_below_line` | 5 通过、1 失败。旧盘中回放基线通过，新规则入参按预期失败。 |
| `tests/components/test_rules.py`（三次） | 5 通过；新增共享求值测试后 5 通过、1 失败；实现后 6 通过。 |
| `tests/components/test_rules.py tests/perception/test_alerts_engine.py tests/perception/test_alerts_level_event.py tests/workbench_api/test_alert_service.py tests/workbench_api/test_alerts_api.py tests/workbench_api/state_repositories/test_legacy_database.py` | 54 通过、6 失败。修复未对齐的历史柱截止与测试契约。 |
| `tests/perception/test_scanner.py::test_manual_scan_evaluates_a_composite_rule_only_on_visible_confirmed_bars tests/workbench_api/test_alert_service.py tests/workbench_api/state_repositories/test_legacy_database.py --tb=short` | 15 通过、1 失败。扫描规则尚未接线。 |
| `tests/perception/test_scanner.py --tb=short` | 11 通过。 |
| `tests/workbench_api/test_alert_service.py::test_migrated_intrabar_touch_replay_freezes_at_the_legacy_event tests/workbench_api/test_scanner_api.py::test_manual_json_rule_scan_returns_a_frozen_hit --tb=short` | 1 通过、1 失败。迁移回放的画线 fixture 有冗余字段，已修复。 |
| `tests/perception/test_alerts_engine.py::test_composite_rule_can_trigger_on_first_confirmed_cross_after_warmup tests/workbench_api/test_alert_service.py::test_migrated_intrabar_touch_replay_freezes_at_the_legacy_event --tb=short`（两次） | 1 通过、1 失败，后 2 通过。显式上穿在首个有足够历史的确认柱可触发。 |
| 上述 Python 回归命令（首次） | 199 通过、2 失败。修复验收测试的旧字段断言后仅 TCP 用例失败。 |
| `tests/acceptance/test_m1_solo.py::test_m1_solo_acceptance_flow tests/perception/test_alerts_engine.py::test_composite_rule_can_trigger_on_first_confirmed_cross_after_warmup --tb=short` | 2 通过。 |

| 命令 | 结果 |
| --- | --- |
| `pnpm -C dsh install --offline --frozen-lockfile` | 离线仓库缺少 tarball。 |
| `pnpm -C dsh install --offline --frozen-lockfile --store-dir /Users/dikui/Library/pnpm/store --config.package-import-method=copy` | 离线仓库仍缺少 tarball，改用主仓库已安装依赖的副本，工作区包链接指向本 worktree。 |
| `pnpm -C dsh --filter @talaria/desk-ui test src/features/alerts/model.test.ts` | 19 通过、1 失败，复现组合规则被覆盖。 |
| `pnpm -C dsh --filter @talaria/desk-ui test` | 首轮 196 通过、1 失败，修复测试常量引用后 197 通过、0 失败。 |
| `pnpm -C dsh typecheck`、`pnpm -C dsh build` | 首轮缺少扫描规则的显示名称，补齐后均通过。 |
| `pnpm -C dsh --filter @talaria/dsh-plugin test` | 24 通过、9 失败，全部在 `listen 127.0.0.1` 被沙箱拒绝。 |
| `PYTHONPATH=$PWD/core/src /Users/dikui/Project/Talaria/.venv/bin/python -c "import sys; from talaria.cli.main import main; sys.exit(main(sys.argv[1:]))" contract-check --write` | 生成成功。 |
| 同一命令去掉 `--write` | 通过，生成物零漂移。 |
| `grep -rnE --exclude='*.pyc' --exclude-dir=node_modules --exclude-dir=__pycache__ --exclude-dir=dist 'AlertCondition\|IndicatorSeed\|alert_condition' core/src dsh/packages contracts tests` | 无匹配，退出码 1。 |
| `git diff main...HEAD`、`git diff main --check` | HEAD 相对主分支无差异，工作区空白检查通过。 |

插件的 TCP 失败项包括快照 POST、凭据文件、HTTP 错误处理、只读工具、标的周期不匹配、画线写入、预警草案写入、协同咨询和简报。严格静态前端审计与基线均为 65 项既有问题，按文件与规则编号比较结果一致。沙箱内未验证真实交易所流、浏览器交互、DSH 桌面宿主及正在独立开发的账户进程集成。账户调用路径由组件注入的确认柱和盘口事件测试覆盖。

#### 改动文件

- `.scratch/alerts-and-scanning/issues/01-json-rules-alerts.md`
- `contracts/schemas/alert_condition.schema.json`
- `contracts/schemas/alert_create.schema.json`
- `contracts/schemas/alert_list_response.schema.json`
- `contracts/schemas/alert_patch.schema.json`
- `contracts/schemas/alert_record.schema.json`
- `contracts/schemas/alert_trigger_list_response.schema.json`
- `contracts/schemas/alert_trigger_record.schema.json`
- `contracts/schemas/dsh-tools/create_alert.tool.json`
- `contracts/schemas/rule.schema.json`
- `contracts/schemas/scanner_focus_item.schema.json`
- `contracts/schemas/scanner_focus_response.schema.json`
- `contracts/schemas/scanner_scan_request.schema.json`
- `contracts/schemas/scanner_scan_response.schema.json`
- `contracts/schemas/scanner_trigger.schema.json`
- `contracts/schemas/scanner_trigger_list_response.schema.json`
- `core/src/talaria/cli/contracts_io.py`
- `core/src/talaria/components/__init__.py`
- `core/src/talaria/components/rules.py`
- `core/src/talaria/components/runtime.py`
- `core/src/talaria/feed/supervisor.py`
- `core/src/talaria/foundation/alerts/__init__.py`
- `core/src/talaria/foundation/alerts/contracts.py`
- `core/src/talaria/foundation/annotations/tools.py`
- `core/src/talaria/foundation/rules.py`
- `core/src/talaria/foundation/scanner/contracts.py`
- `core/src/talaria/perception/alerts/engine.py`
- `core/src/talaria/perception/scanner/engine.py`
- `core/src/talaria/workbench_api/alert_service.py`
- `core/src/talaria/workbench_api/routes/scanner.py`
- `core/src/talaria/workbench_api/runtime.py`
- `core/src/talaria/workbench_api/scanner_service.py`
- `core/src/talaria/workbench_api/state_repositories/alert_migration.py`
- `core/src/talaria/workbench_api/state_repositories/alerts.py`
- `dsh/packages/talaria-contracts/src/generated.ts`
- `dsh/packages/talaria-desk-ui/src/features/alerts/AlertPanel.tsx`
- `dsh/packages/talaria-desk-ui/src/features/alerts/model.test.ts`
- `dsh/packages/talaria-desk-ui/src/features/alerts/model.ts`
- `dsh/packages/talaria-desk-ui/src/features/scanner/model.ts`
- `dsh/packages/talaria-dsh-plugin/create_alert.tool.json`
- `dsh/packages/talaria-dsh-plugin/test/bridge.test.mjs`
- `tests/acceptance/test_m1_solo.py`
- `tests/components/test_rules.py`
- `tests/perception/test_alerts_engine.py`
- `tests/perception/test_alerts_level_event.py`
- `tests/perception/test_scanner.py`
- `tests/workbench_api/openapi_before_router_split.json`
- `tests/workbench_api/state_repositories/test_legacy_database.py`
- `tests/workbench_api/test_alert_service.py`
- `tests/workbench_api/test_alerts_api.py`
- `tests/workbench_api/test_annotations_api.py`
- `tests/workbench_api/test_key_level_registry.py`
- `tests/workbench_api/test_scanner_api.py`
- `tests/workbench_api/test_strategy_subscription.py`

#### 提交状态

`git add -- <逐个列出的任务路径>` 因沙箱无法在主仓库的 `.git/worktrees/alerts-01/` 创建 `index.lock`，退出码 128。改动保存在当前工作区，分支仍指向 `dbe8ee1`。

### 审查修复（2026-11-22）

- 在 `21b9b69^` 实际回放 SMA(1) 的乱序首确认。四个事件后的累计触发数为 `[0, 0, 0, 1]`，唯一触发价为 `120`。迁移回归同时核对次数、时刻和消息。旧槽修订重算最新可见柱时，几何、指标和关键位均绑定这根观察柱。提前收到确认包时，冻结、触发记录和冷却均受数据可见时刻约束。
- 显式上穿和下穿服从整条规则的边沿、断流恢复和冷却基线。规则文档字符串约定了真、假、未知三值逻辑。跨事件分支保留同一组可观察输入的边沿，缺值不会补成上一事件的行情。回踩在整条规则命中时消费，继续停留在突破一侧期间只报一次。
- 手动规则命中记录为本地扫描结果。协同启用时，对命中标的继续运行原有五类数值门禁，只有门禁产出的异动能进入协同路由，并与自动扫描共用每类异动的冷却与去重。扫描按观察柱收盘时刻选关键位版本，按实际求值截止时刻冻结规则证据。重放保留缺口、历史修订和算子预热，指标使用工作台生效的参数。实时持仓量和标记价供规则和门禁共用。资金费率序列接收连接器返回的实际结算点，ticker 的预估费率仍可通过当前事件字段比较。连续衍生品冷库接入留给 data-access/08。
- 规则的常量、倍数和几何比率在模型入口校验十进制字符串。创建或编辑预警时校验关键位 ID 和可见版本中的类型。`drawing_price` 和 `level_price` 可作为比较两侧的操作数，区域可选 `lower` 或 `upper`，默认上沿。同步更新 JSON Schema、TypeScript 类型、DSH 工具契约和工作台规则摘要。
- `Ctx` 提供当前事件、画线解析器、关键位版本和回踩状态。历史运行按可见时刻读取画线，并按观察柱收盘时刻读取关键位。三个入口在同一迟到收盘场景产生相同观测价与证据时刻。
- DSH 工具 Schema 单测已按版本化规则更新，保留草案权限与对象闭合断言。M2 录制文件仍发送旧阈值条件，已换为语义相同的 `last_price >= "70000"` 事件规则。原双平面验收断言保留。单独通过录制宿主替身和进程内 ASGI 路由验证了草案创建（201 DRAFT）与人工确认（200 ARMED）。

| 最终验证 | 结果 |
| --- | --- |
| 组件、预警、扫描、服务、API、OpenAPI 与 DSH Schema 定向 pytest | 180 通过 |
| `PYTHONPATH=$PWD/core/src /Users/dikui/Project/Talaria/.venv/bin/python -m pytest tests -q -p no:cacheprovider` | 938 通过，15 失败，1 项 fixture 初始化错误，1 项按配置取消选择。14 项失败及初始化错误均因沙箱拒绝监听回环端口；另 1 项因 DSH 无权写入用户配置目录的 cordis.yml。M2 完整双平面场景在申请端口时中止。 |
| `pnpm -C dsh --filter @talaria/desk-ui test` | 197 通过 |
| `pnpm -C dsh --filter @talaria/dsh-plugin test` | 24 通过，9 项在监听 127.0.0.1 时收到 EPERM |
| `pnpm -C dsh typecheck`、`contract-check`、`git diff --check` | 均通过，生成契约零漂移 |

新增的共用历史实现在 `core/src/talaria/perception/confirmed_history.py` 和 `rule_derivatives.py`。其余改动位于组件与规则契约、预警与扫描引擎、快照和工作台装配、契约生成物及相应回归测试。改动保留在工作区，未提交。
