# 04 — 模拟盘：实时数据入口、已确认草案提交与账本展示

**What to build:** 同一个交易方案换成 `--mode paper` 就接实时行情跑模拟盘；图表上人工下单或 agent 会诊产出的草案在交易员确认后，以不可变版本提交给对应账户进程；账户页显示子账户的持仓、回合与成交，图表显示活跃止损止盈线（只读）。

**Blocked by:** data-access/06 — 策略实时订阅；03 — 默认成交插件；06 — agent 写的 Python 策略：批准与隔离运行。

**Source of truth:** ADR-0015；`docs/architecture.md` §6.1、§6.2、§5.5（`EXECUTION_BARRIER`）。

**Status:** resolved

- [ ] 实时数据入口插件基于 data-access/06 的订阅；失去实时订阅或订阅被拒时按配置切 `REDUCING`。
- [ ] `--mode paper` 对方案里的每个子账户取排他锁（01），并调用 06 的批准检查。
- [ ] 草案保存与提交分开：服务保存可编辑草案；交易员确认后服务提交已确认版本（草案 ID、版本号、内容哈希、确认人与时刻）；账户宿主按 `(草案 ID, 版本号)` 幂等接收，重新冻结快照，过期返回 `DRAFT_STALE`，否则送定仓与风控。
- [ ] 服务只读账本，提供账户页数据；服务不写账本（测试断言）。
- [ ] 测试：未确认的草案无法进入账户进程；确认后又修改的草案需重新确认，旧版本不生效；同一版本重复投递只处理一次；过期草案返回 `DRAFT_STALE`。
- [ ] 浏览器检查并截图：宽度 1440 与 390、浅色与深色；无持仓、多个回合、长标的名。
- 拖动活跃止损线按收紧/放宽分别路由到成交插槽与风控插槽，以后单独做。

## Comments

### 实现记录

- `talaria account run <scheme.yaml> --mode paper` 在原账户宿主装配 `live_ws`。以 `slots.data: {plugin: live_ws, params: {instrument: BTC-USDT-PERP, timeframe: 1m}}` 为例。成交始终消费确认的 1m 柱，策略按方案周期读取 `ctx`，衍生品复用策略订阅。保留交易方案的子账户锁和策略批准检查。发现订阅拒绝、断开、启动超时或 1m 数据中断时，`reduce_on_disconnect` 默认将账户切入 `REDUCING`，撤掉未成交入场单。此次运行保持该状态。
- 图表可保存人工草案，会诊结果可带其证据快照存为协同草案。常驻服务的 SQLite 写队列保存编辑及确认，确认绑定版本、内容 SHA-256、交易员和时刻。账户通过原服务凭据按子账户和标的领取。待领取时再次编辑会撤销旧确认。领取是提交的边界，之后的同版本重投返回原回执。宿主在入场前重新冻结证据，检查可见 1m 柱和热端进行中柱。越过入场价带、触及止损或超过有效期返回 `DRAFT_STALE`。新候选走原定仓、风控和成交，候选与账本记录 `deliberation_ms`，决策时钟从重新冻结时刻起算。
- 每个子账户使用独立 SQLite 账本。常驻服务只读投影持仓、回合和成交。账户页保留十进制字符串，图表按当前标的投影只读 `EXECUTION_BARRIER`。账户和方向的选择器沿用系统原生菜单。前端与服务契约生成物、OpenAPI 基准已同步。
- 任务单的只读屏障范围与 ADR-0015 及架构 §5.5、§6.1、§6.2 无直接冲突。§5.5 还描述屏障拖动，本单按任务范围只提供活跃止损、止盈投影。草案过期通过账户回执保存。
- 空仓且无未完成回合时可以续写账本，恢复余额和草案回执。已有未完成回合时启动会报错，执行器在途状态尚无跨进程恢复能力。草案过期校验使用分钟高低价，跨草案原快照的首根柱可能包含此前价格，采用保守判定。连续生产运行的延迟和账本增长尚待测量。

#### 实际验证

下表 pytest 命令均使用前缀 `PYTHONPATH=$PWD/core/src /Users/dikui/Project/Talaria/.venv/bin/python -m pytest`，以及 `-q -p no:cacheprovider`。结果依次为通过 / 失败。

| pytest 参数 | 结果 |
| --- | --- |
| `tests/workbench_api/test_paper_api.py` | 0 / 2 → 2 / 0。首轮验证路由尚缺 |
| `tests/account/test_paper.py` | 0 / 6 → 5 / 1 → 6 / 1。依次完成接收、订阅拒绝策略和续写账本 |
| `tests/account/test_paper.py tests/workbench_api/test_paper_api.py` | 1 项导入收集错误 → 9 / 0。增加 CLI 和投影场景后 11 / 1，定位回合投影缺子账户字段 |
| `tests/account/test_paper.py tests/workbench_api/test_paper_api.py tests/workbench_api/test_openapi.py` | 12 / 1 → 13 / 1 → 14 / 0。更新 OpenAPI 基准，修正断线场景的确认柱样本 |
| `tests/account tests/workbench_api/test_paper_api.py tests/workbench_api/test_strategy_subscription.py tests/foundation/test_import_boundaries.py tests/foundation/test_contracts.py tests/cli/test_contract_check.py -k 'not production_worker and not seatbelt_denies'` | 296 / 0，1 跳过、4 项取消选择 |
| 下面的最终定向回归 | 347 / 0，5 项 Seatbelt 用例取消选择 |

```sh
PYTHONPATH=$PWD/core/src /Users/dikui/Project/Talaria/.venv/bin/python -m pytest tests/account tests/workbench_api/test_paper_api.py tests/workbench_api/test_strategy_subscription.py tests/workbench_api/test_openapi.py tests/workbench_api/test_bars_api.py tests/workbench_api/test_key_level_registry.py tests/foundation/test_import_boundaries.py tests/foundation/test_contracts.py tests/cli/test_contract_check.py -q -p no:cacheprovider -k 'not production_worker and not seatbelt_denies and not agent_strategy_can_backtest_without_approval'
```

| 前端或契约命令 | 结果 |
| --- | --- |
| `pnpm -C dsh --filter @talaria/desk-ui test -- src/features/paper/AccountPanel.test.tsx` | 197 通过，新增套件在面板实现前导入失败 |
| `pnpm -C dsh --filter @talaria/desk-ui exec vitest run src/features/paper/AccountPanel.test.tsx` | 面板实现前 1 个套件导入失败，0 个用例执行 |
| `pnpm -C dsh --filter @talaria/desk-ui test` | 两次均 198 通过、0 失败 |
| `pnpm -C dsh --filter @talaria/dsh-plugin test` | 24 通过、9 失败，均为回环监听 `EPERM` |
| `pnpm -C dsh --filter @talaria/dsh-plugin exec node --test test/panels.test.mjs` | 8 通过、0 失败 |
| `pnpm -C dsh typecheck` | 首轮指出可选属性类型，修复后两次通过 |
| `pnpm -C dsh build` | 两次均通过，主分块超过 500 kB |
| `PYTHONPATH=$PWD/core/src /Users/dikui/Project/Talaria/.venv/bin/python -c "import sys; from talaria.cli.main import main; sys.exit(main(sys.argv[1:]))" contract-check --write` | 生成器补齐元组与类型别名后成功，后续重生成成功 |
| 同一命令去掉 `--write` | 零漂移 |

- `git diff main...HEAD`、工作区差异及新增文件已复核，`git diff --check` 通过。无删除类验收项。额外运行 `grep -rn --include='*.py' '模拟盘实时数据和账本接线由 trading/04 提供' core/src tests`，无匹配，退出码 1。
- UI 静态审计 `python3 /Users/dikui/.codex/plugins/cache/openai-curated-remote/frontend-design-premium/1.4.0/skills/frontend-design-premium/scripts/audit_project.py "$PWD" --mode strict` 报告 67 项，其中本单两处为原生下拉的归属说明。按任务要求将决定记录在本单。

#### 待在本机验证

- Seatbelt 用例为 `tests/account/test_backtest.py::test_agent_strategy_can_backtest_without_approval`，以及 `tests/account/test_strategy_isolation.py` 的两个 `test_production_worker_*` 和 `test_seatbelt_denies_other_quotes_and_protects_registration` 的两个模式。真实交易所与账户进程回环 WebSocket 待验证。
- DSH 回环失败为 `test/bridge.test.mjs` 的 7 个本地 HTTP 测试（第 24、58、104、148、263、297、346 行附近），以及 `test/index.test.mjs` 的会诊和简报提交测试。错误均为 `listen EPERM: operation not permitted 127.0.0.1`。面板测试单独通过。
- `ego-browser` 启动遇到 macOS XPC `Connection invalid`，1440/390 宽度的浅色和深色截图待验收。账户组件的可打开样本在 `/tmp/talaria-trading-04/account-preview.html`，含空仓、多回合、长标的名及第二个有持仓的子账户。
- `git add -- core/src/talaria/account/paper.py` 返回 128，主仓库 worktree 元数据目录的 `index.lock` 写入被拒绝。所有变更保留在 `ticket/trading-04` 工作区，HEAD 为 `c47a59c8196b2fe2b13fd64078f6640193ae43d2`。拟提交信息为 `feat: 接通模拟盘实时行情、交易草案与账本`。

#### 改动文件

- `.scratch/trading/issues/04-paper-trading.md`
- `contracts/schemas/confirmed_draft.schema.json`
- `contracts/schemas/draft_confirmation.schema.json`
- `contracts/schemas/draft_content.schema.json`
- `contracts/schemas/draft_delivery_request.schema.json`
- `contracts/schemas/draft_edit.schema.json`
- `contracts/schemas/draft_evidence.schema.json`
- `contracts/schemas/draft_receipt.schema.json`
- `contracts/schemas/draft_record.schema.json`
- `contracts/schemas/paper_account_view.schema.json`
- `contracts/schemas/paper_fill.schema.json`
- `contracts/schemas/paper_round.schema.json`
- `core/src/talaria/account/contracts.py`
- `core/src/talaria/account/host.py`
- `core/src/talaria/account/ledger.py`
- `core/src/talaria/account/paper.py`
- `core/src/talaria/account/paper_ledger.py`
- `core/src/talaria/account/plugins/bar_sim_1m.py`
- `core/src/talaria/account/plugins/live_ws.py`
- `core/src/talaria/account/plugins/seven_step.py`
- `core/src/talaria/account/scheme.py`
- `core/src/talaria/cli/account.py`
- `core/src/talaria/cli/contracts_io.py`
- `core/src/talaria/foundation/paper.py`
- `core/src/talaria/sdk/_subscription.py`
- `core/src/talaria/workbench_api/app.py`
- `core/src/talaria/workbench_api/routes/paper.py`
- `core/src/talaria/workbench_api/service.py`
- `core/src/talaria/workbench_api/state_repositories/paper.py`
- `core/src/talaria/workbench_api/state_store.py`
- `dsh/packages/talaria-contracts/src/generated.ts`
- `dsh/packages/talaria-desk-ui/src/App.tsx`
- `dsh/packages/talaria-desk-ui/src/features/collaboration/CollaborationPanel.tsx`
- `dsh/packages/talaria-desk-ui/src/features/paper/AccountPanel.test.tsx`
- `dsh/packages/talaria-desk-ui/src/features/paper/AccountPanel.tsx`
- `dsh/packages/talaria-desk-ui/src/features/paper/DraftForm.tsx`
- `dsh/packages/talaria-desk-ui/src/features/paper/ExecutionBarrierLayer.tsx`
- `dsh/packages/talaria-desk-ui/src/features/paper/usePaperQuery.ts`
- `dsh/packages/talaria-desk-ui/src/lib/api.ts`
- `dsh/packages/talaria-desk-ui/src/styles.css`
- `dsh/packages/talaria-desk-ui/src/workbench/WorkbenchPanel.tsx`
- `dsh/packages/talaria-desk-ui/src/workbench/panels.ts`
- `tests/account/test_paper.py`
- `tests/workbench_api/openapi_before_router_split.json`
- `tests/workbench_api/test_paper_api.py`

### 审查修复

基于 `ticket/trading-04` 的 `dfa73fe`。以下记录更新前次实现记录中的投递、过期和续写行为。

| 审查项 | 实现与验证 |
| --- | --- |
| 1、10 | 人工打开草案时按图表标的和周期重新冻结快照，会诊入口沿用会诊快照。审阅页展示证据快照和有效期，确认表单绑定审阅时的版本和 SHA-256。轮询读到新版本时重新挂载表单，清空确认人和提交状态。 |
| 2、3 | 服务在同一 SQLite 事务中写入不可变确认和递增提交序号，先重投已领取项，再按确认顺序领取。账户先提交 `DRAFT_PENDING`，将审批、成交事件、唯一计划 ID、执行检查点和终态回执原子提交，然后才发回执。已提交的重投直接返回原结果。测试分别在提交前及提交后丢回执处中断，随后重启，并在部分成交后再次中断。两个场景均只产生一个计划和两笔正确的部分成交。 |
| 4 | 人工与协同表单均可选择同账户、同标的、同方向的目标回合。确认哈希覆盖回合、止损、止盈、最长持仓时间及移动止损。宿主核对当前屏障，随后走原有 R 定仓和七步风控，按已有敞口、挂单预留与新增数量计算回合总风险。 |
| 5–8 | 实时入口按分钟保存待处理柱，迟到的首次确认可以填补空位，已推进分钟的修订不会再次撮合。缺口通过范围接口补齐。信号上下文保留首次确认柱和它的可见截止时刻，计划抵达成交插槽时仍受实际时间延迟墙约束。订阅遇配额或临时无行情时退避重订；握手补发完毕且分钟序列追平后才接收新草案。资金费预测立即进入审批，结算按持仓时间线入账。行情恢复时继续执行已生效的风控限制。 |
| 9 | 过期窗口从草案创建和原证据截止时刻中较晚者开始，在重新冻结的截止时刻结束。沿同一快照向前翻页，分钟中段检查真实进行中柱。若首分钟整体高低价越界，取实际窗口内的逐笔核对；需要证明区间内价格安全时，校验逐笔成交量与高低价。缺分钟、缺进行中柱或缺必要的逐笔证明时继续待处理。重复领取可以补证。 |
| 11、12 | 每个子账户在自己的锁与 SQLite 事务内写账本、事件序号和版本化 JSON 检查点。检查点包括执行器、挂单余量、持仓成本和保证金、退出屏障、预留、已平回合的持仓时间线、资金费入账记录、交易状态与原因、UTC 日初权益和历史峰值。行情检查点含每个通道的游标、待补分钟和待按时间线交付的衍生品点。草案处理状态由同账本事件恢复。多账户进程按相同的已提交行情检查点共享订阅。正常停机撤销挂单并记录停机降险；恢复时只有停机原因允许恢复交易。 |
| 13、14、16 | 持锁写者可续建初始化中断的空库；续写前核对锁令牌的数据目录和账户集合。旧协同库测试保留历史 `legacy_schema.sql`，检查旧对象与数据、升级后与全新库结构一致、升级库能保存和读回草案。 |
| 15、17 | `slots.data.backtest` 和 `slots.data.paper` 各自接受插件名或带参数的选择。测试用同一份 YAML 运行回测和模拟盘两个入口。账户页接入图表停靠列表，补齐账户图标。 |

同一方案的数据插槽示例。其余插槽及策略批准沿用原方案。

```yaml
slots:
  data:
    backtest:
      plugin: parquet_replay
      params: {instrument: BTC-USDT-PERP, timeframe: 1m, start: 0, end: 60000}
    paper:
      plugin: live_ws
      params: {instrument: BTC-USDT-PERP, timeframe: 1m, history_bars: 500}
```

**旧账本兼容性。** 已平旧回合可以根据历史成交重建资金费归属，并抵扣历史资金费流水。旧临时接收标记后已有明确提交或拒单事件时，重投取回该结果。升级前仍有在途挂单或持仓、且没有执行检查点的旧账本，未记录原批准数量、排队余量或完整执行器状态，启动会指出对应回合并停止。新格式的在途恢复已覆盖。升级前未落盘的历史权益峰值也无法精确补造。

**暂缓的本地鉴权加固。** 确认、领取、回执与普通服务调用共用同一 Bearer。持有服务凭据的本机进程可冒充交易员确认，当前确认人字段不能证明交易员身份。后续应分别绑定交互式交易员身份与账户进程身份，给确认以及指定子账户的领取、回执授予独立权限，审计记录关联实际认证主体。

#### 审查修复验证

- 红测通过公开的账户入口、领取接口、行情迭代器、账本及前端表单复现。观察到的失败包括：先保存的草案抢先领取；确认写入失败后重试撞唯一键；中断标记被当作拒单；跳过迟到分钟；信号读到修订；配额拒绝后没有第二次订阅；当前分钟缺失仍批准；首分钟沿用创建前低点；空库缺表；目录或账户不匹配的令牌仍可打开账本。修复后的回归保留在对应测试模块。
- Python 使用 `PYTHONPATH=$PWD/core/src /Users/dikui/Project/Talaria/.venv/bin/python -m pytest`。按要求运行 `tests -q -p no:cacheprovider` 后，查明 15 项回环监听及 1 项真实 DSH Profile 写入受沙箱限制。排除这 16 项复跑后为 **1257 通过、1 失败、5 跳过、17 未选**。5 项为嵌套 Seatbelt，未选数另含仓库默认排除的在线测试。唯一失败是 `tests/acceptance/test_m1_solo.py::test_m1_solo_acceptance_flow` 等待扫描器的 `LIQUIDITY_ANOMALY` 超时；用 `git archive dfa73fe` 导出的干净基线单独复跑，同样在第 558 行超时。
- 定向命令 `pytest tests/account tests/workbench_api/test_paper_api.py tests/workbench_api/state_repositories/test_legacy_database.py tests/workbench_api/test_strategy_subscription.py tests/workbench_api/test_openapi.py tests/foundation/test_import_boundaries.py tests/cli/test_contract_check.py -q -p no:cacheprovider` 为 **318 通过、5 跳过**。最后缩小重订时所需的历史范围后，再跑 `tests/account/test_paper.py tests/workbench_api/test_strategy_subscription.py tests/sdk` 为 **93 通过**。
- `pnpm -C dsh --filter @talaria/desk-ui test` 为 **211 通过**。`pnpm -C dsh --filter @talaria/dsh-plugin test` 为 **25 通过、9 失败**，9 项均为监听 `127.0.0.1` 遇到 `EPERM`。单独运行插件的 guard 和 panels 两个套件为 **17 通过**。`pnpm -C dsh typecheck`、`pnpm -C dsh build` 成功，构建保留既有的 500 kB 分块提示。`contract-check` 零漂移，`git diff --check` 通过。
- 真实交易所长跑、沙箱外端口与 Seatbelt 场景、1440/390 浅深色截图待本机验收。截图由调用方检查。

审查改动涉及账户宿主、执行门控、账本与新建的 `account/checkpoint.py`，默认成交及风控插件、实时订阅客户端与服务握手、草案仓储和共享契约、账户与图表表单、DSH 停靠注册，以及相应的测试、生成契约与 OpenAPI 基准。工作区保留在 `ticket/trading-04`，HEAD 为 `dfa73fe`。
