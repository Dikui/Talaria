# 02 — 图表规则编辑器与规则输出显示

**What to build:** 在图表页上组合出一条规则，立即在图上看到它的信号标记和引用的指标线，确认后一键存为预警。

**Blocked by:** platform/02 — 前端多页面；01 — JSON 规则与预警升级。

**Source of truth:** ADR-0019；`docs/architecture.md` §5.2、§5.5。

**Status:** resolved

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

### 审查修复（2026-11-22）

- 快照记录确认柱的各次修订、接收时刻，以及冻结时热端仍保留的盘中和通道健康事件。规则预览在独立的 `AlertEngine` 实例上回放。画线随接收时刻更新，衍生品来自冻结的归档修订和实时 ticker。相同毫秒且有接收顺序的 ticker 不会提前出现在前一事件中。迟到首确认、旧槽修订、断流恢复、冷却与重新待命均由实盘引擎处理。预览沿用编辑器创建预警时的重复设置和 60 秒冷却。
- `condition_true` 每个显示槽保留最后一次成立的观察。`would_trigger` 保留每次触发及其毫秒时刻。两组各有图上标记，明细可用键盘聚焦，列出柱时间、观察时间、价格和类型。FEED_GAP 从图表 dataList 过滤后，规则指标线仍在该处断开。
- 此分支原有快照存储包含柱和衍生品 Parquet，关键位注册表只驻内存。现将每次实际登记的完整关键位版本、发布时间和服务启动记录持久化到快照清单数据库，每个快照绑定冻结时的发布序号。历史回放按事件到达时已发布的版本还原注册表，再按观察槽收盘时间选版。注册表淘汰、相同截止时刻的替换及重启均有回归覆盖。旧快照缺少回放记录或关键位历史版本时返回明确错误；部分时段缺失时界面说明覆盖范围。
- 新建图表指标引用随规则固定起点快照；保存和重启后继续使用该起点。迁移规则保留原预警窗口，并在编辑器和图例显示实际根数。正常追加只推进已有算子状态，多通道共用同一次递推；修订改变历史输入、旧规则的递归窗口换起点时按对应语义重新播种。断开请求、同一面板的新预览均会取消旧计算。
- 编辑器按面板内的联动组、标的与周期保留收盘和盘中草稿。画线选择读取全标的已持久化目录，平移和缩放不影响引用。关键位历史查询与当前保存校验各自反馈。历史中从未出现的 ID 会返回错误，当前已失效的 ID 可查看历史结果，保存按钮提示原因。保存成功后按实际预警 ID 判断状态，删除后可再次保存。

#### 回归与性能

- 复现覆盖输出缺少两组结果、迟到旧槽、收盘与接收之间画线改版、早到确认包、逐笔衍生品、关键位发布时间与淘汰、重启基线、旧 ID、请求取消、VWAP 起点和编辑器状态。新增用例保存在 `tests/workbench_api/test_rule_replay.py`、`test_key_level_registry.py`、`test_indicators_api.py` 及前端 `src/features/rules/*.test.*`。
- 1000 槽、四个 BOLL 条件的回放红测用时 7.073 秒。修复后，以 1000 根确认柱走冻结快照 HTTP 预览接口，三次分别为 0.303、0.301、0.299 秒，均得到 951 根条件成立柱。性能测试保留 2 秒上限。
- 修改集中在预警引擎与算子上下文、快照构建和存储、关键位注册表、规则预览及画线路由、图表规则编辑器、KLineChart 指标配置和对应的生成契约。新增实现文件为 `core/src/talaria/perception/snapshot_events.py` 和 `dsh/packages/talaria-desk-ui/src/features/rules/ruleDrafts.ts`。

#### 浏览器验收入口

用独立页 `/desk/chart` 或 DSH「图表」打开「图表规则」；工作台 `/desk` 共用该编辑器，保存后的预警可在 `/desk/alerts` 查看。

1. 在 1440 和 390 px、浅色和深色下查看空规则、三层组合规则、全空预热指标。展开「命中明细」，用 Tab 聚焦条目，对照图上条件成立和实际触发的标记、时间及价格。
2. 在同一联动组分别填写收盘与盘中草稿，切换标的和周期再切回。选择画线后平移至视窗以外。保存预警，在预警页删除，再回到图表查看保存按钮。
3. 用含 FEED_GAP 的数据检查指标线。用先存在后移除的关键位检查历史预览与禁用保存提示，再用缺少关键位历史版本的快照检查错误文案。对照图上 VWAP 起点、旧预警的根数标签和预热提示。

浏览器截图、真实交易所实时流和 DSH 桌面宿主由委托方验收。盘中历史仅覆盖冻结时保留下来的事件。缺少原始同毫秒接收序号的归档记录按存储次序打破平局。

#### 最终验证

| 命令或范围 | 结果 |
| --- | --- |
| `PYTHONPATH=$PWD/core/src /Users/dikui/Project/Talaria/.venv/bin/python -m pytest tests -q -p no:cacheprovider` | 1168 通过，16 失败，1 项 fixture 初始化错误，5 跳过，1 取消选择。14 项失败及初始化错误均遇到回环端口监听被沙箱拒绝，1 项 DSH 配置测试无权写入用户目录。另 1 项 M1 单机验收等待流动性异动超时，已在干净的 `5572559` 单独复现。 |
| 规则组件、预警引擎与服务、快照、画线、指标预览、关键位注册表、OpenAPI 和 DSH 工具契约定向 pytest | 175 通过。 |
| `pnpm -C dsh --filter @talaria/desk-ui test` | 33 个测试文件，206 项通过。 |
| `pnpm -C dsh typecheck`、`pnpm -C dsh build` | 均通过。Vite 提示独立页面主包超过 500 kB。 |
| `contract-check`、`git diff --check` | 生成物零漂移，空白检查通过。 |

分支为 `ticket/alerts-02`，HEAD 为 `5572559`，修复保留在工作区。
