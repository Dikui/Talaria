# 01 — 回放编排：批量启动账户进程跑回测（确定性策略）

**What to build:** `talaria replay` 对一个或多个交易方案、多个区间批量启动账户进程，汇总每次回放的净收益、回撤、交易笔数与已入账的手续费、资金费率，结果可以放在一起对比。首批只支持确定性策略。

**Blocked by:** trading/02 — 默认定仓与风控插件；trading/03 — 默认成交插件。

**Source of truth:** `docs/architecture.md` §7；ADR-0015、ADR-0007、ADR-0009。

**Status:** ready-for-agent

- [ ] 每次回放一个 `run_id` 目录；并发运行互不写同一文件，不占模拟盘子账户锁。
- [ ] 汇总报告读取各账本，不重算成交。
- [ ] 含 agent 的方案在入口被拒绝并说明原因（认知成本与证据等级以后做）；报告注明只覆盖确定性策略。
- [ ] 测试：两个方案、两个区间的批量回放结果与单独运行一致（规范化事件与分录比较）。

## Comments

### 实现记录

- `talaria replay a.yaml b.yaml --range START END --range START END --workers 2` 对方案和区间的每个组合启动独立的 `account run --mode backtest --replay` 子进程。方案使用 `parquet_replay`，范围沿用账户宿主的 UTC 毫秒语义，省略 `--range` 时使用各方案的范围，显式设置的 `history_start` 继续作为预热下界。默认串行运行。每次回放由账户宿主创建独立的 `runs/<run_id>/` 及运行配置、账本，测试中持有同一子账户的模拟盘锁也能完成四组回放。
- 标准输出的 JSON 按输入顺序列出每次回放，范围标为 `deterministic_only`。交易笔数按归零的出场成交计完整仓位回合。账本分录只读汇总同币种子账户的已结毛利、成交手续费、强平罚金及资金费净收付。最大回撤按分录追加顺序计算已入账净收益曲线从峰值下跌的绝对金额。迟到结算和修订使用各自的入账分录，这一口径的收益和回撤只计已实现部分。
- 整批在启动前使用原有策略代码登记和快照规则校验来源。已批准的 agent 代码、未登记代码及登记后修改的代码会被拒绝，账户进程加载前复核同一规则。架构 §7 限制 `replay` 仅运行确定性策略，ADR-0015 的 agent Python 回测仍通过独立的 `account run --mode backtest` 提供。本任务验收项均为新增行为。
- 两个方案和两个区间的规范化执行事件与复式分录逐组等于独立回测，覆盖盈利、亏损、无交易、手续费和出场后才入账的资金费。批量准入依据登记的代码来源和哈希，人工策略的确定性由方案提供者保证。复现比较以相同冷库内容、代码和生效配置为前提，长区间资源开销未测。

#### 实际验证

下列 pytest 命令使用前缀 `PYTHONPATH=$PWD/core/src /Users/dikui/Project/Talaria/.venv/bin/python -m pytest`。

| pytest 参数 | 结果 |
| --- | --- |
| `tests/orchestration/test_replay_runner.py -q -p no:cacheprovider` | 首轮 1 项 fixture 初始化错误（结算类别实参应为枚举），修正后 1 项因 CLI 缺失失败。随后依次 1、5、6 项通过，各轮 0 失败 |
| `tests/orchestration/test_replay_runner.py -k rejects_agent -q -p no:cacheprovider --tb=short` | 0 通过、4 失败、1 排除，复现启动前准入缺失 |
| `tests/orchestration tests/account tests/foundation/test_import_boundaries.py tests/cli/test_cli.py tests/cli/test_contract_check.py -k 'not up_dual_keeps_fast_path_when_dsh_fails and not up_dual_keeps_fast_path_when_dsh_binary_missing' -q -rs -p no:cacheprovider` | 285 通过、0 失败、5 跳过、2 排除，1 条既有 Starlette 弃用警告 |

- 按任务说明的命令运行 `contract-check`，生成物零漂移。相同 `PYTHONPATH` 和解释器下，`-m talaria replay --help` 通过。已复核 `git diff main...HEAD` 与工作区差异。
- 需在沙箱外 macOS 验证的 Seatbelt 用例为 `test_agent_strategy_can_backtest_without_approval`、`test_production_worker_imports_dependencies_under_seatbelt`、`test_production_worker_cannot_read_cold_parquet_or_open_network` 及 `test_seatbelt_denies_other_quotes_and_protects_registration` 的两组参数。定向回归命令中的两项排除需要绑定回环端口。全量测试未运行。
- 首次逐路径暂存返回 128，沙箱禁止在主仓库的 worktree Git 元数据目录创建 `index.lock`。改动保留在 `ticket/backtest-01` 工作区，HEAD 为 `c47a59c8196b2fe2b13fd64078f6640193ae43d2`。拟提交信息为 `feat: 实现确定性策略批量回放`。

### 审查修复

- 批次启动子进程前固定各交易方案的生效配置和整批共用的工作台指标默认值。一次 SQLite 读事务记录涉及标的的 K 线与衍生品清单代号、文件路径和哈希，两个清单均写入每份 `run-config.json`。策略使用的跨周期 K 线、衍生品和撮合输入都从记录的清单取数。清单更新或压实后仍可使用保留的文件重放，原文件被回收时报出缺失路径。缺少必要清单或代码哈希的旧产物会明确拒绝复现。
- 预检记录人工登记的 Python 文件插件快照 SHA-256、来源及路径。子进程和产物重放均核对记录，再加载同一份校验过的归档。预检后重新登记的不同版本也会被拒绝。
- 父进程预分配 `run_id`，写入配置并创建独立的 `runs/<run_id>/state` 后才启动账户进程。回测插件通过上下文获得 `run_id`、状态目录和只读行情读取器。运行结束后写入 `run-status.json`，子进程提前退出时由父进程补写失败状态。CLI 按输入顺序输出各任务状态、`run_id` 和错误。失败账本保留供排查，汇总只读取成功且有信号柱的账本。同批有失败任务时，成功结果仍输出，命令退出码为 2。
- 账本报告分别列出交易手续费 `fees`、资金费净收付 `funding` 和强平罚金 `liquidation_fees`。`signal_bars` 记录实际完成求值的信号柱数。零信号柱标为 `empty_sample: true`，收益及交易笔数为空。

#### 实际验证

- 对八项发现先观察红测，再通过对应回归。额外验证了隔离子进程从固定清单读取稀疏的历史衍生品，沿用实际返回行数和响应字节数的限制。
- 账户、回放编排、SDK 和数据层定向回归共 429 项通过、5 项 Seatbelt 跳过。实测冷库压实切换清单后仍可重放，删除退役文件后明确失败。
- 使用指定解释器与 `PYTHONPATH=$PWD/core/src` 运行 `-m pytest tests -q -p no:cacheprovider`，通过 `-k` 排除 15 项已知回环端口用例及 1 项需写入沙箱外配置目录的 DSH 用例。得到 1211 项通过、5 项 Seatbelt 跳过、17 项取消选择（含 1 项默认 online 用例）、1 项失败。
- 唯一失败为 `tests/acceptance/test_m1_solo.py::test_m1_solo_acceptance_flow`，等待 `LIQUIDITY_ANOMALY` 超时。将 `d624596` 导出到临时目录单独复跑，在同一行复现。
- `python -m talaria contract-check` 零漂移，`git diff --check` 通过。长区间性能未测。改动保留在工作区。
