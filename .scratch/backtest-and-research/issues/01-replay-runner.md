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
