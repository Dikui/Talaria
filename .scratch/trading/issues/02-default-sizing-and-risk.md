# 02 — 默认定仓与风控插件：按止损距离定仓与七步风控

**What to build:** 交易方案选 `sizing: r_unit` 与 `risk: seven_step` 后，定仓插件按止损距离给出数量与依据，风控插件核对并经过七步风控、单一仓位回合不变量、风险预留与回撤熔断；被拒的计划留下原因记录。

**Blocked by:** 01 — 账户进程宿主。

**Source of truth:** `docs/architecture.md` §6.3；ADR-0015（单一仓位回合与 `R` 定仓为默认插件规则，插槽交接）；GLOSSARY 的 Risk Unit、Position Round、Risk Reservation、Trading State。

**Status:** ready-for-agent

- [ ] 定仓插件 `size` 与 `preview_sizing`；风控插件 `evaluate_candidate(candidate, sizing, ...)` 与 `approve_barrier_widening`。风控只批准或拒绝定仓给出的数量，不重算。
- [ ] `MarginValuation` 的 `crypto_perp` 实现：逐仓预估强平价、全仓账户级压力测试，按标记价估值；03 共用。
- [ ] 七步风控每一步有独立测试；反向开仓返回 `OPPOSITE_POSITION_OPEN`；加仓沿用回合屏障且总风险不超 `max_single_r_pct`。
- [ ] 同一子账户串行审批与风险预留：两笔并发计划不会基于同一旧状态同时放行。
- [ ] 回撤熔断按阈值切 `REDUCING` / `HALTED`。
- [ ] 测试：只把定仓换成一个按名义金额定仓的测试插件，风控仍按其数量核对 `max_single_r_pct` 并能拒绝超限计划。
- [ ] `preview_sizing` 单次耗时 < 5 ms（记录实测值）。

## Comments

### 实现记录

- 交易方案注册了 `r_unit` 和 `seven_step`。两者共用按子账户配置的 `account_risk`，生效值随回测配置归档。空账本以成对分录初始化本金。定仓按最差入场价、止损距离和双边摩擦预算计算数量，按标的步长向下取整。试算返回定仓依据、预估强平价和止损至强平的距离。
- 风控保留原始 `SizingDecision`，依次检查账户状态和屏障几何、单回合风险、同向相关性簇、市场时段和委托规格及可选静默窗口、保证金和强平、含手续费与滑点及资金费率的净赔率，再签发不可变计划。加仓复用回合屏障和持仓期限，并计入活跃持仓和在途加仓的总风险。硬止损放宽复核同一风险预算、当前标记价和强平安全距离。
- 账户宿主串行审批同一子账户的候选及止损放宽。已接受的计划按剩余未成交量预留风险、保证金和回合，入账、撤单或终态回报后释放对应占用。拒单原因入账。回撤以 UTC 日首净值和历史峰值为基线，标记价或账本变动触线时切 `REDUCING` 或 `HALTED`，撤销未成交入场单，硬熔断可按配置全平。
- 架构 §6.3 将全仓估值标为规划，本单实现该接口。市场包通过 L0 的只读 `MarginValuation` 接收 `MarginPortfolio`、标记价或压力情景价格，供 trading/03 复用。`crypto_perp` 采用线性合约与固定 0.5% 维持保证金率的研究模型。逐仓保证金按入场名义金额和配置杠杆估算；全仓对同时触及各回合止损安全边界的情景做压力测试。待成交订单的假设浮盈不计入当前可用保证金。资金费率使用截止时刻的已知不利费率与配置缓冲中的较大值。
- 标记价和资金费率由数据入口通过 `MarketEvent.derivative_points` 送入，按事件时间及可获取时点检查。回放用注入标记价和原有 Parquet 柱验证了正向成交与未来标记价拒单。生产衍生品冷库接线、默认成交执行和实时模拟盘留待已有的 data-access/08、trading/03 与 trading/04。研究保证金模型未核对真实交易场所的分层费率与强平结果。本单没有删除类验收项。

#### 实际验证

下列 pytest 参数均使用同一命令前缀。

```sh
PYTHONPATH=$PWD/core/src /Users/dikui/Project/Talaria/.venv/bin/python -m pytest
```

| pytest 参数 | 通过 / 失败 / 收集错误 |
| --- | --- |
| `tests/account/test_default_risk.py tests/packs/test_margin_valuation.py -q -p no:cacheprovider` | 红测 0 / 0 / 2，补齐接口后 6 / 0 / 0 |
| `tests/account/test_default_risk.py -q -p no:cacheprovider` | 依次 0 / 0 / 1、16 / 1 / 0、17 / 4 / 0、24 / 1 / 0，最终由下方局部回归覆盖 |
| `tests/account/test_default_risk.py tests/account/test_execution.py -q -p no:cacheprovider` | 48 / 0 / 0 |
| `tests/account/test_backtest.py::test_scheme_runs_default_sizing_and_risk_with_point_in_time_marks -q -p no:cacheprovider` | 接线红测 0 / 2 / 0，修复由下方局部回归覆盖 |
| `tests/account tests/packs/test_margin_valuation.py tests/packs/test_instruments.py tests/foundation/test_import_boundaries.py -q -p no:cacheprovider` | 依次 133 / 0 / 0、137 / 0 / 0、139 / 0 / 0 |

按任务单的命令运行 `contract-check`，契约生成物零漂移。局部回归没有 TCP 绑定或网络失败。试算在一个已开回合、一个在途回合和两个有效标记价的合成账户上进行。每种保证金模式预热 200 次、测量 5000 次。逐仓中位数 0.0094 ms、p99 0.0105 ms、最大 0.0215 ms。全仓中位数 0.0129 ms、p99 0.0137 ms、最大 0.0195 ms。

最后一次配置隔离调整后，同一局部回归再次通过 139 项。`git diff --check` 通过。已复核 `git diff main...HEAD`、工作区改动与新增文件。逐路径暂存时 Git 退出码为 128，沙箱拒绝创建主仓库 worktree 元数据中的 `index.lock`。改动保留在 `ticket/trading-02` 工作区。当前 HEAD 为 `3930d8a15dcefe530c5a24094151b2e03e59a14a`，拟用提交信息为 `feat: 实现默认定仓与七步风控插件`。
