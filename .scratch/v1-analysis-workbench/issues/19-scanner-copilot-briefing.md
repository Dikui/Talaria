# 19 — 异动扫描协同简报（`CO_PILOT_CONSULT` 通道）

**What to build:** 开启协同简报的扫描规则命中后，经单标的冷却锁与全局并发令牌桶限流唤醒后台会话，智能体复用只读工具生成结构化异动简报（触发原因、关键价位参考、风险提示，无有效观点时 `Abstain`）；交易员在简报收件箱点击即可切到对应快照图表，或把简报带入协同对话继续深究。

**Blocked by:** 15 — 确定性异动扫描器；17 — 协同会诊

**Source of truth:** [需求文档](../spec.md)、[领域词汇表](../../../GLOSSARY.md)、[系统分层架构规范](../../../docs/architecture.md)、[ADR-0004](../../../docs/adr/0004-dual-mode-intraday-cognition-and-scanner-gate.md)

**Status:** ready-for-agent

- [x] 实现 15 预留的 `CO_PILOT_CONSULT` 路由：先过单标的冷却锁与全局最大并发令牌桶，超限请求降级为纯数值异动卡，绝不定时轮询模型。
- [x] 简报会话绑定触发快照的 `snapshot_id`，Token 用量照常经宿主侧幂等留痕。
- [x] 看盘页提供简报收件箱，支持一键切换主图到对应 `Instrument`、周期与快照视窗，或带入右侧协同对话。
- [x] 离线测试覆盖批量异动下的冷却与令牌桶限流、超限降级、`--solo` 无宿主时自动降级为纯数值卡；真实模型简报为沙箱外可选冒烟。
- [x] 浏览器界面验收（均附截图）：宽度 `1440` 与 `390`、浅色与深色、简体中文下检查简报收件箱与跳转交互；覆盖长简报摘要折叠与无简报空态。


## Comments

### 实现

- 领域层新增 `core/src/talaria/foundation/briefings/`：`contracts.py` 定义 `BriefingStatus`、`BriefingContent`、`ScannerBriefing`、列表/带入/回调请求响应；`prompt.py` 生成只读、绑定单一 `snapshot_id` 的结构化简报提示词，并解析宿主返回的 JSON（`trigger_reason` / `key_levels` / `risks` / `evidence_ids`）。
- 服务层新增 `core/src/talaria/workbench_api/briefing_service.py`。`offer(trigger)` 是同步门禁，不排队、不阻塞扫描线程：宿主 URL 为空、快照为空、同 `instrument` 冷却未到、`inflight >= max_concurrency`、令牌桶不足，任一条件即返回 `False`；只有全部通过才记冷却、占并发、创建 PENDING 并异步派发 `/talaria/briefing`。
- 冷却按标的独立计时，默认 5 分钟；令牌桶容量取 `max(1, int(rate_per_minute // 60))`（默认 6/min 即容量 1），按 `rate_per_minute/60` 每秒补充；全局并发另由 `max_concurrency` 限制，默认 1。被门禁拒绝的 Trigger 由 `TriggerRoutingPolicy` 退回 `LOCAL`，仍生成 15 的纯数值异动卡，只是不产生简报行、不调用模型。
- `routing.py` 的 `CoPilotConsultChannel.offer` 兼容 `bool | None`：只有显式 `False` 才降级，旧的返回 `None` 的占位通道仍按接受处理。
- 简报在派发前先落 PENDING，`snapshot_id` 直接取触发冻结快照，提示词要求只读取该快照；宿主结果经 `POST /api/scanner/briefings/{id}/result` 回写，文本 JSON 必须引用证据目录内 ID，未知证据判 `EVIDENCE_CONFLICT`，非法输出判 `MODEL_INVALID_OUTPUT`，超时判 `MODEL_TIMEOUT`，宿主不可达判 `HOST_OFFLINE`；模型按提示词主动返回 `abstain` 时保留其原因码与证据，且证据同样过滤到冻结目录内，不再误判为非法输出。
- Token 用量随简报回调内嵌 `usage`，服务端复用 `token_usage` 表按 `(dsh_session_id, dsh_turn_id)` 幂等入账；重复回调不重复累计。
- 持久化：`state_store.py` 新增 `scanner_briefings`（id、trigger_id、snapshot_id、instrument、timeframe、status、summary、evidence_refs、abstain_reason、created_ts、read_ts）与按时间/未读索引，全部经 `StateWriterQueue` 单写者写入；`created_ts` 取触发时刻。API：`GET /api/scanner/briefings`、`POST /api/scanner/briefings/{id}/read`、`POST /api/scanner/briefings/{id}/consult`（返回预填问题，不重复调用模型）、`POST /api/scanner/briefings/{id}/result`；读公开、写沿用 Bearer。
- DSH 插件新增 `/talaria/briefing`，复用 `ctx.agents` 和只读工具上下文，一轮后回传文本 + `dsh_session_id/dsh_turn_id/usage`；bridge 新增 `submitBriefingResult`。宿主离线、`--solo`、未加载 model 时快速回 `HOST_OFFLINE`，不报错刷屏。
- 前端新增 `features/briefings/`：收件箱列表、未读徽标、状态/相对时间、长摘要折叠、关键位、风险、证据折叠、Abstain/失败卡和空态；头部「简报 N」与右栏第三 tab 同步未读数。点击卡片复用 `locateTrigger` 切主图到 Instrument/周期并进入触发快照视窗；READY 简报可「带入协同」，切到右栏协同并把绑定快照的问题预填进输入框。390 窄屏收件箱为全屏抽屉，隐藏带入协同与发起类入口，只保留阅读和切图。

### 测试与验证

- `uv run pytest -q`：473 passed, 1 deselected（online）。
- `uv run talaria contract-check`：契约生成物零漂移。
- `pnpm -C dsh typecheck`：通过（contracts 与 desk-ui）。
- `pnpm -C dsh --filter @talaria/desk-ui test`：21 files / 148 tests passed；新增简报收件箱折叠、未读、Abstain、空态、只读、跳图 payload 与带入问题绑定。
- `pnpm -C dsh build`：通过（仅 Vite chunk-size 提示）。
- 插件 `node --test dsh/packages/talaria-dsh-plugin/test/{bridge,guard,index}.test.mjs`：18 passed，含 `/talaria/briefing` 一轮与结果回传。
- 离线门禁用例覆盖：同标的批量异动只派发一次、令牌桶耗尽立即拒绝、并发已满拒绝、`host_url=None` / `--solo` 零派发零落库、拒绝后仍由路由降级 `LOCAL`、简报绑定触发 `snapshot_id`、证据过滤/冲突、模型主动 Abstain 原因保留、非法 JSON、超时、usage 重复提交只计一轮。测试用 spy dispatcher 断言调用次数，未启动任何模型进程或定时器。

### 截图

- `/Users/dikui/Project/Talaria/.scratch/screens/ticket-19/1440-light-inbox.png`
- `/Users/dikui/Project/Talaria/.scratch/screens/ticket-19/1440-dark-collapsed.png`
- `/Users/dikui/Project/Talaria/.scratch/screens/ticket-19/1440-bring-to-collab.png`
- `/Users/dikui/Project/Talaria/.scratch/screens/ticket-19/1440-host-offline-abstain.png`
- `/Users/dikui/Project/Talaria/.scratch/screens/ticket-19/1440-empty-state.png`
- `/Users/dikui/Project/Talaria/.scratch/screens/ticket-19/390-readonly-inbox.png`

截图使用 `/tmp/talaria-ticket19-populated` 的 5 条演示简报与空库 `/tmp/talaria-ticket19-empty`，均未入库。

### 已知限制

- 真实宿主模型简报只做了离线/假宿主验证，未在沙箱内跑真实模型冒烟；插件需重启 DSH 并加载新版 `index.js` 后才会响应 `/talaria/briefing`。
- 简报收件箱只保留最近 200 条查询窗口，暂无归档/清理策略；进程在 PENDING 中途退出会留下未完成行，下一次同标的冷却到期前不会自动重试。
- 令牌桶与冷却状态在进程内存，重启清零；当前是单进程单写者语义，不做跨进程分布式限流。
- 390 为只读阅读模式，隐藏「带入协同」；合成窄屏浏览器验收，未覆盖真实移动设备手势。
