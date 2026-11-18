# Agentic trading：确定性执行底座、权限隔离与评测边界

- 调研日期：2026-11-15。只使用实际可访问的一手官方文档、官方源码、作者论文与作者代码。
- 范围：Alpaca 官方 MCP、NautilusTrader、Alpaca 订单语义，以及一个 LLM 时间泄漏 benchmark；不判断策略是否赚钱，不执行交易、登录或收费 API。
- 方法：静态源码阅读与文档核对；**没有启动这些交易系统，也没有执行集成测试或复现实验收益**。访问日期不代表发布日期，更不代表最新稳定版。
- 固定源码快照：Alpaca MCP `main`：[`9daa38cc41d2327467b8dd83cceb06b457074cbc`](https://github.com/alpacahq/alpaca-mcp-server/tree/9daa38cc41d2327467b8dd83cceb06b457074cbc)；NautilusTrader `develop`：[`12982032fb970cabc085f00169c720792c3edecf`](https://github.com/nautechsystems/nautilus_trader/tree/12982032fb970cabc085f00169c720792c3edecf)。**后者是开发分支快照，不能直接视为已发布稳定版承诺。**

## 1. 结论先行

1. **Alpaca MCP 是 broker API 的工具接入层，不是完整的交易安全内核。**它确实在服务端按 toolset 注册白名单，但默认启用全部组；“按组过滤”不等于“严格只读”。`account` 自带改配置，`trading` 把查订单/持仓与下单、撤单、平仓、期权行权放在一起。[A1]；[A2]
2. **paper/live 要在部署层 fail-closed。**源码默认值是 paper，但判断逻辑是“只有 `true/1/yes` 走 paper，其他任何值走 live URL”，而不是“只有明确 `false` 才开启 live”。[A3]
3. **NautilusTrader 更适合充当 Agent 后面的确定性交易内核。**其事件驱动的 Strategy → RiskEngine → ExecutionEngine → adapter，以及 Cache、Portfolio、MessageBus 分工，明显区别于 LLM 的计划/辩论/记忆层；但同构回测/实盘不等于结果一致，也不等于分布式 exactly-once。[N1]；[N2]；[N4]
4. **最大的执行边界是“外部副作用不确定”，不是模型能否输出 JSON。**超时不代表未下单，提交成功不代表已接受或已成交；Nautilus 内置持久化异步，venue 接单与本地 durable commit 之间有窗口。应单独做持久化 intent/attempt 账本、唯一 ID、状态查询和 reconciliation，不依赖 Agent 重试。[A5]；[N4]
5. **LLM 历史回测必须把模型记忆污染与市场数据时间泄漏分开。**Look-Ahead-Bench 提供跨时段表现衰减诊断，但论文/当前 runner 存在需要核对的频率和执行时间约定；本报告只借鉴其警示，不引用绝对收益、不认可“已消除泄漏”的结论。[B1]；[B2]；[B3]

## 2. Alpaca 官方 MCP：真实能力与不能误读的权限

### 2.1 官方事实：服务端注册过滤确实存在，但没有统一严格只读开关

- `_make_filter(allowed_ops)` 只有 operation ID 在允许集合、且不是手写 override 时返回 `MCPType.TOOL`，其余 `EXCLUDE`；`ALPACA_TOOLSETS` 缺失/空字符串返回 `None`，代表所有组；交易 override 只在 `trading` 组启用时注册。文档查询工具另行始终注册。因此它不是只给客户端看的 UI 隐藏规则。[A1]
- 但它是**业务组 allowlist，不是按 HTTP 方法/副作用等级划分的 authorization policy**。源码组成员如下。[A2]

| 工具组 | 已核实的写能力/边界 |
|---|---|
| `account` | 包含 `patchAccountConfig`；不能称为账户只读。 |
| `trading` | 查询订单、查询持仓与撤改单、平仓、期权 exercise/do-not-exercise 同组；另注册股票/加密资产/期权下单工具。 |
| `watchlists` | 创建、修改、删除 watchlist 等写操作。 |
| `locates` | 包含 `createLocates`，不是纯查询。 |
| `assets`、市场数据类、`news` | 本次所读组成员是查询类；可用它们缩小工具暴露面，但不包含账户/订单/持仓的完整只读组合。 |

**推论/建议：**不要写“Alpaca MCP 默认只读”“去掉 trading 就严格只读”“account 是安全只读组”。研究 Agent 若还要读取账户/订单/持仓，应通过自有服务端逐工具 allowlist 或快照 API，只暴露明确无副作用的方法。把 broker 凭据留在执行/数据服务，而非给模型进程。上述代码没有证明 broker key 自身具备只读权限；工具过滤与凭据权限是不同层。[A1]；[A2]；[A3]

### 2.2 官方源码事实：paper 默认安全，非法枚举却不是 fail-closed

关键逻辑是：[A3]

```python
paper = os.environ.get("ALPACA_PAPER_TRADE", "true").lower() in ("true", "1", "yes")
return TRADING_API_BASE_URLS["paper" if paper else "live"]
```

- 未设置：paper；大小写形式的 `true/1/yes`：paper。
- `false`、空值、拼写错误、`paper`、带尾空格的 `true `：均选择 live URL。
- 这是**路由选择风险**，不意味着 paper key 会自动获得 live 权限；官方 paper/live 使用不同 API keys 与 endpoint。[A6]

**建议：**以独立部署配置固定 paper/live endpoint、独立 secret、独立账户；只接受严格枚举，未知值拒绝启动；live 在进程启动时额外验证显式许可、账户 ID、endpoint 和 risk policy hash。Agent 无权修改环境变量或密钥。

### 2.3 网络暴露与 prompt injection

- CLI 默认 `stdio`、HTTP host 为 `127.0.0.1`；HTTP/SSE 配有 host/origin 防护。当前 `FastMCP(...)` 构造和 CLI 路径没有配置远端用户身份认证/逐调用交易审批。**Host/Origin guard 不等价于用户身份认证或交易授权。**[A4]
- `TrustBoundaryMiddleware.on_call_tool` 先 `await call_next(context)`，之后把结果包成 `untrusted_tool_output`；新闻/外部文本的警示明确提到 prompt injection、凭据盗取、tool hijacking。它是**结果信任标注**，不是调用前授权、交易风控，也不是证明模型不会受注入影响的安全边界。[A7]

**建议：**网页、新闻和工具返回一律是资料，不是配置/授权指令；只能流入 evidence 字段。授权策略、live 开关、可交易账户、风险限额从独立可信配置加载，不允许通过自然语言、检索结果或 Agent 自我批准来改写。不得把无认证 HTTP MCP 直接公网暴露。

### 2.4 下单封装不是交易 OMS

- `_post_order` 直接 `POST /v2/orders`，处理 HTTP 错误及 `ReadTimeout`；`client_order_id` 是可选参数。下单工具注解本身声明 `readOnlyHint=False`、`destructiveHint=True`、`idempotentHint=False`。这一调用路径未见持久化命令账本、人工审批绑定或账户级组合风险预算。[A5]
- 源码超时文案说：订单可能已提交，检查 open orders；如果设置 `client_order_id`，同 ID 重试会被 API 拒绝为重复。**但 broker 官方 FAQ 更保守：超时后不要重发或把订单标记为取消，应在 broker dashboard/Alpaca Support 或交易团队确认后再行动。**不能把 wrapper 文案扩张为“无条件安全重试”或 exactly-once 保证。[A5]；[A8]

**建议：**在自己的命令语义中强制稳定 `intent_id` / `client_order_id`，超时进入 `UNKNOWN`，查询同一个 ID、等待 trade updates，仍不明则冻结冲突意图并升级处理；不要换新 ID 重下，也不要看到 open-orders 没有记录就推断未成交。

## 3. NautilusTrader：执行内核该有哪些组件

### 3.1 官方架构与 Agent 接口位置

| 官方组件 | 已核实职责 | 对 Agentic 系统的建议映射（非官方产品承诺） |
|---|---|---|
| `NautilusKernel` | 共享核心组件、clock、环境、生命周期与 messaging 配置。[N1] | 单一执行运行时边界，不把研究编排塞入此内核。 |
| `DataActor` / `Strategy` | Actor 消费/发布数据与事件、访问 cache；Strategy 增加订单管理。Rust 是 `pub trait Strategy: DataActor`。[N2] | 研究输出经桥接成为信号/目标组合，再由可信 Strategy 生成订单。Actor 的类型分工本身不是恶意代码沙箱。 |
| `RiskEngine` | 下单/改单前校验价格、数量、余额、notional、reduce-only、交易状态与速率。[N3] | 硬门禁；不能由“风险 Agent 的同意”替代。 |
| `ExecutionEngine` / `ExecutionClient` | 订单/持仓状态、命令路由、成交事件、reconciliation；adapter 对接 venue。[N1]；[N4] | 负责 OMS 和外部副作用，而不是研究 Agent 自己调用 broker。 |
| `Cache` / `Portfolio` | 前者维护 instruments/accounts/orders/positions 等索引；后者派生余额、仓位、margin、PnL、exposure。[N1] | 风险以受控状态快照计算，不从对话记忆推断仓位。 |
| `MessageBus` | typed command/event、pubsub、request-response；可选 Redis streams 是 transport，durable recovery 属于 cache/event store。[N1] | 用 schema 化消息连接组件；不要将 pubsub 当事务账本。 |

典型直通路径是 `Strategy → RiskEngine → ExecutionEngine → ExecutionClient → venue`；事件返回更新 Cache、Portfolio，并通知 Strategy。模拟订单或执行算法可先经过 OrderEmulator / ExecutionAlgorithm；cancel/query 不经过 RiskEngine。[N2]；[N3]

**同构但不等价：**Backtest 使用历史数据+模拟成交；Sandbox 使用实时数据+模拟成交；Live 使用 venue 连接（可 paper，也可真钱）。共享内核/策略接口降低迁移差异，但数据到达顺序、延迟、撮合及 adapter 行为仍会改变结果。[N1]

**线程边界：**核心的策略回调、risk、cache、消息 dispatch 在单线程；网络、日志、持久化在后台。官方不支持同一进程并行运行多个 LiveNode/BacktestNode。[N5] 因此建议 LLM 推理、网页研究与慢 I/O 放独立 worker/process，通过有 TTL 的消息交接，不能阻塞撮合/风控事件循环。

### 3.2 风控状态必须读到精确语义

| 状态/配置 | 官方事实 | 设计含义 |
|---|---|---|
| `ACTIVE` | 正常 submit/modify/cancel/query。[N3] | “正常”仍需要配置实际限额，不代表系统自动知道你的资本预算。 |
| `REDUCING` | 只允许符合条件的单个 `reduce_only` 提交，以及 cancel/query；position ID、账户、方向、数量必须符合缓存中的开仓；拒绝订单列表和修改。[N3] | 应急减仓不是任意“卖单”。 |
| `HALTED` | 只允许 cancel/query；拒绝新提交/修改，也拒绝已释放的保护性止损。[N3] | kill switch 不是“自动平仓”，也不代表保护单仍会发出去；必须单独定义停机、撤单、降风险流程。 |
| `RiskEngineConfig.bypass=true` | 跳过 risk checks、速率与 trading-state 检查；提交源码直接转执行。[N3]；[N6] | live 配置必须禁止 Agent 修改此开关；独立配置审计应失败关闭。 |

源码默认下单/改单速率各为 `100 / 1s`，每 instrument 的 `max_notional_per_order` 默认为空 map。[N6] **这不是“开箱即符合资金风险偏好”**。组合集中度、杠杆预算、日内损失阈值、数据陈旧阈值、跨策略净额限额等，若业务需要，应在固定策略或独立硬风控层明确补齐；不应凭内置 RiskEngine 名字推断这些已经覆盖。

补充边界：REDUCING/HALTED 规则作用于进入 risk engine 的命令；官方特别说明 emulator 内部 held-order 的本地修改不经过这层限制。[N3] 上述行为要按**实际选用版本和 adapter**测试，不能只测常规直通路径。

### 3.3 恢复、幂等与持久化：最容易过度承诺的部分

**官方明说的限制：**[N4]

1. submit/modify/cancel **不保证 exactly-once delivery**。只有 venue 协议的稳定请求身份、去重窗口或真正幂等语义才能使重试安全。`cancel-all` 的重复执行甚至可能撤掉两次调用之间新出现的订单。
2. 官方列出“未完全符合 retry gate”的情况：Bybit、OKX、Kraken spot 的部分命令在瞬时失败下可能重复发送，并声明这不是完整清单。这不是对所有 adapter 的统一保证。
3. 订单及 origin 在执行客户端调用前进入内存 cache/排队写入，但 enqueue failure 只记录日志、不阻止发送；Redis/PostgreSQL backing 异步处理，成功 enqueue 不代表 durable commit。
4. venue 已收到订单、但本地 order/origin 尚未 durable 时可能崩溃。启动 reconciliation 可按 venue 报告恢复，但 venue 历史不完整可能使完整事件链不可重建。可选 event store 同样异步，不 gate dispatch。
5. execution-client call 返回成功或发出 `OrderSubmitted` 都不证明 venue acceptance；`SUBMITTED`、`PENDING_UPDATE`、`PENDING_CANCEL` 是等待解决中的状态。

**官方 reconciliation 边界：**显式 open/flat position report 是 exposure 证据；缺报告/null quantity 不等于 flat。启用 reconciliation、且报告落入所配置过滤范围时，启动必须对齐或者 fail-closed，未解决报告阻止策略/actor 启动。[N7]

**建议：**Nautilus 提供了状态机、事件与对账机制，但仍应在应用边界增加“发送前持久化命令+attempt”的账本、账户单写者、同意图去重、未知状态隔离和定期 broker reconciliation。**这个本地账本也不能凭空把 venue 变成 exactly-once**；它只是保留了失败后判定/对账所需的稳定身份与证据。

## 4. 官方 broker 事实：paper 与 live 的证明边界

Alpaca 官方文档提供 `client_order_id` 查询和 WebSocket trade updates；查询/异步事件是跟踪生命周期的必要途径，不是把 HTTP response 当作最终持仓。[A8]

Alpaca paper 是独立 key/endpoint、API 形状相同，订单不送真实交易所；它不模拟市场冲击、订单信息泄漏、延迟滑点、队列位置、价格改善、监管费和股息。模拟成交量不受 NBBO 展示数量严格约束，可能成交真实市场不能立即承接的数量。[A6]

**因此：**paper 适合验证工具权限、schema、订单状态流和对账，不足以证明可实现收益、容量或真实成交质量。推进顺序应是历史 replay → paper 集成/故障注入 → 独立授权的受限 live；本次仅做研究，没有授权或执行上述交易。

## 5. 时间泄漏 benchmark：只借鉴诊断，不背书收益

### 作者事实

[Look-Ahead-Bench 论文 v1](https://arxiv.org/html/2601.13770v1)（2026-01-20，Mostapha Benhenda）不是只问模型历史事实，而是在 agentic trading workflow 中比较两个时期相对 buy-and-hold 的 alpha 差：`alpha_decay = alpha_P2 - alpha_P1`。实验是五只科技股，P1 为 2021-04 至 2021-09，P2 为 2024-07 至 2024-12；论文描述月频目标权重再平衡。[B1]

作者主站仓库是展示前端，但 README 另链接了实际 [AI Hedge Fund benchmark fork](https://github.com/benstaf/aihedgefund_lookaheadbench/tree/29fd48c3f12483a645b545c6881378f8d7f20786)，含 `alpha_decay` 计算与 backtest runner。不能仅凭展示站就断言作者没有公开运行代码。[B2]

### 本次静态核查的限制与警示

- 当前 runner 的 `BacktestEngine` 使用 `pd.date_range(..., freq="B")` 逐工作日运行，与论文“月频 target weights”表述不能直接等同；需作者明确实验 commit、入口、参数和数据快照。[B3]
- 该路径查询到 `current_date_str` 的价格，把同一日期传给 Agent，又以取到的末行 `close` 执行。这里只能确认**代码缺乏清晰的“决策可见时刻→下一可成交时刻”分离**；是否实际前视取决于 API 日期包含性和数据时刻，未经跑数不能断言每一笔都泄漏。[B3]
- 财务指标 API 请求是 `report_period_lte=end_date`，本地读取路径没有另加 `published_at/available_at <= decision_time` 过滤。报告期不等于公开可获得时间；仍需核实上游 API 的 point-in-time 语义，不能仅靠请求参数名宣称无时间泄漏。[B4]
- 论文中模型 cutoff、专有 PiT 模型去污染效果是作者设定/主张，本次未独立证实。两个短窗口和五只科技股不足以把 alpha decay 归因于记忆污染：市场 regime、样本选择、执行模型也会影响它。[B1]

**建议的评测合同：**分别记录 market-event time、首次 published/available time、ingested time、decision time、order-eligible time；禁止当日已知 close 信号在同一 close 无延迟成交。冻结模型版本/prompt/采样参数、知识来源和数据快照；使用滚动样本外、跨 regime、模型 cutoff 后的 forward/paper 测试，并与等权、规则策略和不使用 LLM 的相同执行管线比较。收益指标之外，同时验收时间约束违反率、风险拒单率、重复单率、UNKNOWN 持续时间和 reconciliation 差异。

## 6. 给 Talaria 的最小接口与权力隔离（设计建议，不是上述项目现成功能）

```text
研究 Agent（网页/新闻/因子/解释；无 broker 写凭据）
  └─ propose_intent(targets, evidence, decision_asof, expiry)
       ↓ schema + 数据时效校验
持久化 Intent Ledger（intent_id、输入快照、模型/策略版本、审批指纹）
       ↓ 可信 policy/必要时人工授权；不能批准任意未来变化
确定性 Portfolio/Risk Gateway（额度、仓位、价格、频率、live capability）
       ↓ 固定订单 ID + 发送前记录 attempt
OMS / Nautilus Strategy → RiskEngine → ExecutionEngine → venue adapter
       ↑ acceptance / fill / cancel / reject / UNKNOWN / reconciliation
只读执行回执与 broker 快照 → 研究层
```

接口至少携带：`intent_id`、`strategy_version`、`account_alias`、`instrument_id`、目标仓位或明确 action、`decision_asof`、`expires_at`、`input_snapshot_ref`、`risk_policy_version`、`approval_ref`。金额/数量使用精确十进制与明确币种；LLM confidence/rationale 仅作研究元数据，不可解锁额度。执行层在最终发送前重新检查账户状态、当前报价/时效、剩余风险预算；审批绑定 canonical intent hash，参数变化后旧审批失效。

**必须隔离的权力：**研究 Agent 可以提出、撤回尚未发送的意图，读取状态，解释被拒原因；不能设 live、改 risk/bypass、注入 API keys、直接写账本终态、把 UNKNOWN 改成 canceled、或通过同一提示词给自己批准。紧急停止与减仓由独立可信控制面执行，清楚区分 stop-new-risk、cancel-open-orders、reduce-position。[A2]；[A3]；[A7]；[N3]；[N4]

**最低验收用例：**重复 intent；超时但已受理；partial fill 后断线；ACK/fill 乱序或重复；取消与成交竞态；进程在发送后/落盘前崩溃；broker 有仓位但本地空 cache；旧行情/过期审批；非法 paper 值；HALTED 下 protective stop；Agent 从新闻收到“关闭风控”注入。前述源码揭示这些不是边缘文案问题，而是应在接真实资金之前显式验收的系统边界。[A3]；[A5]；[A7]；[N3]；[N4]；[N7]

## 一手来源与固定定位

所有以下链接均为官方组织/文档或论文作者公开资料；源码锚定本次实际读到的 SHA。文档站内容可能变化，重要 Nautilus 结论同时给固定源码文档锚点。

### Alpaca

[A1]: https://github.com/alpacahq/alpaca-mcp-server/blob/9daa38cc41d2327467b8dd83cceb06b457074cbc/src/alpaca_mcp_server/server.py#L80-L88
[A2]: https://github.com/alpacahq/alpaca-mcp-server/blob/9daa38cc41d2327467b8dd83cceb06b457074cbc/src/alpaca_mcp_server/toolsets.py#L9-L159
[A3]: https://github.com/alpacahq/alpaca-mcp-server/blob/9daa38cc41d2327467b8dd83cceb06b457074cbc/src/alpaca_mcp_server/server.py#L101-L132
[A4]: https://github.com/alpacahq/alpaca-mcp-server/blob/9daa38cc41d2327467b8dd83cceb06b457074cbc/src/alpaca_mcp_server/cli.py#L26-L86
[A5]: https://github.com/alpacahq/alpaca-mcp-server/blob/9daa38cc41d2327467b8dd83cceb06b457074cbc/src/alpaca_mcp_server/overrides.py#L24-L109
[A6]: https://docs.alpaca.markets/docs/paper-trading
[A7]: https://github.com/alpacahq/alpaca-mcp-server/blob/9daa38cc41d2327467b8dd83cceb06b457074cbc/src/alpaca_mcp_server/security.py#L22-L104
[A8]: https://docs.alpaca.markets/docs/working-with-orders

- [A1：服务端 route filter](https://github.com/alpacahq/alpaca-mcp-server/blob/9daa38cc41d2327467b8dd83cceb06b457074cbc/src/alpaca_mcp_server/server.py#L80-L88)；[server 构造、toolset 默认全部、override 与 docs 注册：142–212](https://github.com/alpacahq/alpaca-mcp-server/blob/9daa38cc41d2327467b8dd83cceb06b457074cbc/src/alpaca_mcp_server/server.py#L142-L212)；[官方过滤测试：768–775](https://github.com/alpacahq/alpaca-mcp-server/blob/9daa38cc41d2327467b8dd83cceb06b457074cbc/tests/test_server_construction.py#L768-L775)。
- [A2：各业务组 operation IDs 与 None=all](https://github.com/alpacahq/alpaca-mcp-server/blob/9daa38cc41d2327467b8dd83cceb06b457074cbc/src/alpaca_mcp_server/toolsets.py#L9-L159)。
- [A3：凭据 header、paper 枚举与空工具组解析](https://github.com/alpacahq/alpaca-mcp-server/blob/9daa38cc41d2327467b8dd83cceb06b457074cbc/src/alpaca_mcp_server/server.py#L101-L132)。
- [A4：CLI transport、bind host、host/origin protection](https://github.com/alpacahq/alpaca-mcp-server/blob/9daa38cc41d2327467b8dd83cceb06b457074cbc/src/alpaca_mcp_server/cli.py#L26-L86)。
- [A5：下单/超时处理与 tool hints](https://github.com/alpacahq/alpaca-mcp-server/blob/9daa38cc41d2327467b8dd83cceb06b457074cbc/src/alpaca_mcp_server/overrides.py#L24-L109)。
- [A6：官方 paper trading 模拟范围和局限](https://docs.alpaca.markets/docs/paper-trading)。
- [A7：不可信输出提示与调用后的 envelope 包装](https://github.com/alpacahq/alpaca-mcp-server/blob/9daa38cc41d2327467b8dd83cceb06b457074cbc/src/alpaca_mcp_server/security.py#L22-L104)。
- [A8：订单 client ID、trade updates、超时 FAQ](https://docs.alpaca.markets/docs/working-with-orders)。

### NautilusTrader

[N1]: https://github.com/nautechsystems/nautilus_trader/blob/12982032fb970cabc085f00169c720792c3edecf/docs/concepts/architecture.md#L65-L156
[N2]: https://github.com/nautechsystems/nautilus_trader/blob/12982032fb970cabc085f00169c720792c3edecf/docs/concepts/execution/index.md#L19-L99
[N3]: https://github.com/nautechsystems/nautilus_trader/blob/12982032fb970cabc085f00169c720792c3edecf/docs/concepts/execution/index.md#L186-L312
[N4]: https://github.com/nautechsystems/nautilus_trader/blob/12982032fb970cabc085f00169c720792c3edecf/docs/concepts/execution/policies.md#L121-L207
[N5]: https://github.com/nautechsystems/nautilus_trader/blob/12982032fb970cabc085f00169c720792c3edecf/docs/concepts/architecture.md#L470-L523
[N6]: https://github.com/nautechsystems/nautilus_trader/blob/12982032fb970cabc085f00169c720792c3edecf/crates/risk/src/engine/config.rs#L47-L59
[N7]: https://github.com/nautechsystems/nautilus_trader/blob/12982032fb970cabc085f00169c720792c3edecf/docs/concepts/execution/policies.md#L220-L256

- [N1：核心组件与三种环境](https://github.com/nautechsystems/nautilus_trader/blob/12982032fb970cabc085f00169c720792c3edecf/docs/concepts/architecture.md#L65-L156)；[直通下单事件流：230–266](https://github.com/nautechsystems/nautilus_trader/blob/12982032fb970cabc085f00169c720792c3edecf/docs/concepts/architecture.md#L230-L266)；[官网架构](https://nautilustrader.io/docs/latest/concepts/architecture/)。
- [N2：Strategy、RiskEngine、ExecutionEngine 路由](https://github.com/nautechsystems/nautilus_trader/blob/12982032fb970cabc085f00169c720792c3edecf/docs/concepts/execution/index.md#L19-L99)；[Rust Strategy trait 与 submit_order：108–224](https://github.com/nautechsystems/nautilus_trader/blob/12982032fb970cabc085f00169c720792c3edecf/crates/trading/src/strategy/mod.rs#L108-L224)；[DataActor 能力：3–13](https://github.com/nautechsystems/nautilus_trader/blob/12982032fb970cabc085f00169c720792c3edecf/docs/concepts/actors.md#L3-L13)。
- [N3：风险校验、account selection、HALTED/REDUCING/bypass](https://github.com/nautechsystems/nautilus_trader/blob/12982032fb970cabc085f00169c720792c3edecf/docs/concepts/execution/index.md#L186-L312)；[Rust trading-state gateway：1933–2009](https://github.com/nautechsystems/nautilus_trader/blob/12982032fb970cabc085f00169c720792c3edecf/crates/risk/src/engine/mod.rs#L1933-L2009)。
- [N4：重试不保证 exactly-once、adapter 未完全符合、异步持久化窗口](https://github.com/nautechsystems/nautilus_trader/blob/12982032fb970cabc085f00169c720792c3edecf/docs/concepts/execution/policies.md#L121-L207)；[执行客户端调用与 dispatched 标记：2618–2633](https://github.com/nautechsystems/nautilus_trader/blob/12982032fb970cabc085f00169c720792c3edecf/crates/execution/src/engine/mod.rs#L2618-L2633)。
- [N5：核心单线程、后台持久化、crash-only 恢复](https://github.com/nautechsystems/nautilus_trader/blob/12982032fb970cabc085f00169c720792c3edecf/docs/concepts/architecture.md#L470-L523)；[并行 node 需分进程：709–727](https://github.com/nautechsystems/nautilus_trader/blob/12982032fb970cabc085f00169c720792c3edecf/docs/concepts/architecture.md#L709-L727)。
- [N6：风险配置字段与默认值](https://github.com/nautechsystems/nautilus_trader/blob/12982032fb970cabc085f00169c720792c3edecf/crates/risk/src/engine/config.rs#L47-L59)；[bypass 直接转发提交：584–588](https://github.com/nautechsystems/nautilus_trader/blob/12982032fb970cabc085f00169c720792c3edecf/crates/risk/src/engine/mod.rs#L584-L588)。
- [N7：启动持仓报告的 authoritative / fail-closed 范围](https://github.com/nautechsystems/nautilus_trader/blob/12982032fb970cabc085f00169c720792c3edecf/docs/concepts/execution/policies.md#L220-L256)。

### 作者 benchmark

[B1]: https://arxiv.org/html/2601.13770v1
[B2]: https://github.com/benstaf/lookaheadbench/blob/1b7a7e81077a0478ca64a0c83da24477be336ea0/README.md#L26-L34
[B3]: https://github.com/benstaf/aihedgefund_lookaheadbench/blob/29fd48c3f12483a645b545c6881378f8d7f20786/src/backtesting/engine.py#L180-L273
[B4]: https://github.com/benstaf/aihedgefund_lookaheadbench/blob/29fd48c3f12483a645b545c6881378f8d7f20786/src/tools/api.py#L95-L133

- [B1：论文摘要页](https://arxiv.org/abs/2601.13770)；[完整论文 v1](https://arxiv.org/html/2601.13770v1)。
- [B2：作者展示站 README 指向 runner](https://github.com/benstaf/lookaheadbench/blob/1b7a7e81077a0478ca64a0c83da24477be336ea0/README.md#L26-L34)；[alpha decay 实现：108–146](https://github.com/benstaf/aihedgefund_lookaheadbench/blob/29fd48c3f12483a645b545c6881378f8d7f20786/src/backtester_lookahead.py#L108-L146)。
- [B3：逐日循环、同日 end_date 和 close 执行路径](https://github.com/benstaf/aihedgefund_lookaheadbench/blob/29fd48c3f12483a645b545c6881378f8d7f20786/src/backtesting/engine.py#L180-L273)。
- [B4：财务指标 report_period_lte 请求与缓存](https://github.com/benstaf/aihedgefund_lookaheadbench/blob/29fd48c3f12483a645b545c6881378f8d7f20786/src/tools/api.py#L95-L133)。
