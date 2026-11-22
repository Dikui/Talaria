# 02 — 图表规则编辑器与规则输出显示

**What to build:** 在图表页上组合出一条规则，立即在图上看到它的信号标记和引用的指标线，确认后一键存为预警。

**Blocked by:** platform/02 — 前端多页面；01 — JSON 规则与预警升级。

**Source of truth:** ADR-0019；`docs/architecture.md` §5.2、§5.5。

**Status:** ready-for-agent

- [ ] 编辑器按 JSON 规则 Schema 生成表单（算子、参数、比较、逻辑组合），不提供自由文本代码。
- [ ] 规则输出由后端计算，经 KLineChart 异步 `calc` 显示。
- [ ] 一键存为预警，预警页可见。
- [ ] 浏览器检查并截图：宽度 1440 与 390、浅色与深色；空规则、嵌套三层的长规则、引用的算子返回全 `null`（预热期）。

## Comments

### 实现记录

- 图表工作台和 DSH 图表面板共用编辑器。从 JSON 规则 Schema 和已注册算子生成求值时机、节点、操作数、比较、通道及参数控件。可嵌套且／或／非，按规则契约约束深度和节点数。编辑器保留十进制字符串，引用画线与关键位时使用当前图表的真实 ID。
- `POST /api/snapshots/{snapshot_id}/rule-preview` 复用 `Ctx`、`compile_rule` 和指标默认值。按柱收盘与可获取时间求值，画线按观察时点选版，关键位按收盘时刻选版。最多预览已加载窗口的最近 1000 个时间槽。信号和指标线通过 KLineChart 异步 `calc` 按 K 线时间戳对齐，预热和缺失值保留 `null`。
- 「保存为预警」经现有创建接口保存预览固定后的规则，直接进入待命状态。图表的预警列表立即刷新，独立预警页沿用既有轮询。预览接口只读取现有快照和引用数据。
- ADR-0019 与架构 §5.2、§5.5 的求值时机和绘制方式适用。预览绑定图表快照。快照缺少可完整回放的盘中事件序列，事件规则可保存并预览已收盘的引用指标，盘中信号标记留待事件序列可用。历史关键位取决于注册表保留的版本，快照中缺少的旧柱修订也会限制可复原的历史。

#### 验证

Python 测试使用 `PYTHONPATH=$PWD/core/src /Users/dikui/Project/Talaria/.venv/bin/python -m pytest`，均附 `-q -p no:cacheprovider`。

| 路径及筛选 | 结果 |
| --- | --- |
| `tests/workbench_api/test_indicators_api.py::test_rule_preview_aligns_server_signals_and_referenced_indicators` | 先 0 通过、1 失败（路由缺失），后 1 通过、0 失败。 |
| `tests/workbench_api/test_indicators_api.py -k rule` | 5 通过、0 失败，12 未选中。 |
| `tests/components/test_rules.py tests/components/test_history.py tests/perception/test_alerts_engine.py tests/perception/test_alerts_level_event.py tests/perception/test_scanner.py tests/workbench_api/test_indicators_api.py tests/workbench_api/test_key_level_registry.py tests/workbench_api/test_alert_service.py tests/workbench_api/test_alerts_api.py tests/workbench_api/test_openapi.py tests/foundation/test_import_boundaries.py tests/cli/test_cli.py::test_contract_check_has_zero_drift` | 213 通过、0 失败，含迟到确认柱、全空预热、非法算子与参数、预警入库及列表读取。 |

| 命令 | 结果 |
| --- | --- |
| `pnpm -C dsh --filter @talaria/desk-ui test src/features/rules/ruleChart.test.ts` | 缺实现时 1 个套件失败，首次实现 0 通过、1 失败，修正后 1 通过、0 失败。 |
| `pnpm -C dsh --filter @talaria/desk-ui test src/features/rules/RuleEditor.test.tsx` | 缺实现时 1 个套件失败，接通后 1 通过、0 失败。 |
| `pnpm -C dsh --filter @talaria/desk-ui test` | 两次均为 199 通过、0 失败。 |
| `pnpm -C dsh --filter @talaria/desk-ui typecheck`、`pnpm -C dsh typecheck` | 均通过。 |
| `pnpm -C dsh build` | 通过。Vite 提示独立页面主包超过 500 kB。 |
| `PYTHONPATH=$PWD/core/src /Users/dikui/Project/Talaria/.venv/bin/python -c "import sys; from talaria.cli.main import main; sys.exit(main(sys.argv[1:]))" contract-check` | 先两次用 `--write` 生成，随后检查零漂移。 |
| `audit_project.py dsh/packages/talaria-desk-ui --mode strict` | 临时配置沿用现有原生选择控件。比较脚本修正报告字段后，基线和工作区均退出 1，各有 7 项既有问题，新增 0 项。 |
| `git diff main...HEAD`、`git diff --check` | 分支尚无本任务的提交，已另查工作区差异，空白检查通过。 |

#### 待验证与提交状态

- 1440／390 px、浅色／深色，以及空规则、三层长规则、全空指标的实际浏览器截图由委托方检查。当前验证覆盖进程内 API、表单渲染和图表计算回调。真实行情和 DSH 桌面宿主未实测。本单无删除类验收项。
- `git add -- core/src/talaria/workbench_api/rule_preview.py` 退出 128。沙箱拒绝在主仓库元数据目录创建 `.git/worktrees/alerts-02/index.lock`。所有改动保留在 `ticket/alerts-02` 工作区，HEAD 为 `017eec5de8611465c5d15addd194beb9bbc700dc`。
