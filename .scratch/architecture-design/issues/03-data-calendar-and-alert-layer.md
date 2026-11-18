# 设计数据层、事件日历与盘面预警感知模块接口

Labels: wayfinder:grilling
Type: grilling
Status: resolved
Blocked by: 01, 02

## Question

结合已确立的软件分层与外部数据源能力边界，数据与感知层内部的模块切分与公开接口契约应如何设计？具体需敲定：
1. 冷热一体时点数据存储（实时 `HotStream` 内存窗口与 `Parquet + DuckDB + SQLite` 冷快照库）的统一读写接口与跨品种 `Market Pack` 数据源插件协议；
2. `Catalyst Calendar`（宏观与财报事件日历）的双时间数据模型（预期发生时刻 vs 实际公布时刻）及历史回放防前视查询规则；
3. `Watchlist`（自选观察池）与独立毫秒级 `Alert`（价格阈值、技术指标条件、画线突破）预警及异动扫描引擎的状态模型与触发分发接口（如何同时驱动工作台实时提醒与智能体唤醒）。

## Answer

按推荐方案定型数据与盘面感知层（L1 `perception`）的内部模块、数据源插件特征与公开契约：

### 1. 冷热一体时点存储与 `Market Pack` 数据流插件协议

1. **数据源连接器与流级能力声明（`MarketDataConnector` + `FeedCapabilityTrait`）**：
   - 每个 `Market Pack` 注册的 Adapter 必须显式声明不可变 `FeedCapabilitySpec`：`feed_id`（如 `BYBIT_LINEAR`、`BINANCE_USDM`、`HYPERLIQUID_PERP`、`ALPACA_IEX`、`ALPACA_SIP`——作为数据集主键一部分，禁止无标记混拼）、`supported_streams`、`max_rest_kline_lookback_bars`、`has_rest_oi_history`、`has_ws_oi_stream` 与可选 `archive_backfill_driver`。
   - **针对已核实场所边界的默认推荐组合**：
     - 加密永续基准源优先启用 **Bybit V5 Linear**（单公共 WS + REST 免密钥闭环全历史 K 线、`Mark Price`、`Funding` 与 `5min+` OI）与 **Binance USD-M**（近 30 天走 `/fapi` REST/WS + 实时低频轮询 `/fapi/v1/openInterest` + 超 30 天自动路由 `data.binance.vision` 批量归档并校验 SHA-256）；**Hyperliquid** 启用实时 WS 持续本地落盘 Parquet，规避 REST `candleSnapshot` 5,000 根截断。
     - 美股日内默认隔离实时 `ALPACA_IEX`（受 30 标的订阅配额调度）与历史延迟 `>=15m` 的 `ALPACA_SIP`，复权事件单独存表、不在原始 K 线上静默覆写。
2. **单写者不可变冷库发布与冷热无缝读取接口（`PerceptionStore`）**：
   - **冷端发布顺序**：抓取原始报文落盘 `data/raw/`（计 `content_hash`）→ 标准化写入临时 staging Parquet → 完整性/单调性/精度质检通过后原子切换至不可变 generation Parquet 并 `fsync` → 在 `data_manifest.sqlite` 短事务内原子提交清单版本与抓取游标（崩溃至多遗留未引用孤儿文件，绝不暴露半成品批次）。
   - **热端内存环（`HotStreamBuffer`）**：仅服务 `Shadow Run`，按标的维护最新 BBO、进行中/刚闭合 K 线（显式区分 `is_closed` 与迟到修正版本 `bar_revision`）、实时 `Mark/Index/Funding/OI` 及实时新闻流；闭合批次定期异步归档进冷库。
   - **统一只读深接口**：
     - `freeze_snapshot(req: SnapshotRequest) -> EvidenceSnapshot`：固定 `universe`、`cutoff_ts`、`temporal_mode`（`HISTORICAL_PIT` 约束 `ts_available <= cutoff_ts`；`OBSERVED_REPLAY` 同时约束 `ts_available <= cutoff_ts` 且 `ts_committed <= cutoff_ts`）、`feed_id` 与质量门禁，绑定显式 Parquet 文件列表（由 `DuckDB` 只读下推查询）及热端 `<= cutoff_ts` 已提交切片。
     - `read_aligned_series(snapshot_id, query) -> AlignedSeriesBundle`：统一输出**前导 `null` 等长索引对齐**的时间序列及显式缺失状态码（区分休市 `SESSION_CLOSED`、停牌 `HALTED`、无成交 `NO_TRADES` 与断流异常 `FEED_GAP`，严禁把断流静默填零）。
     - 错误契约显式区分 `SnapshotNotFound`、`TemporalCoverageUnknown`、`StaleFeedError`、`UnsupportedCapability` 与 `QualityRejected`。智能体与策略层仅持有只读快照查询权，不持有采集凭证或任意 SQL/外网抓取权。

### 2. `Catalyst Calendar` 双时间数据模型与防前视规则

1. **双时间事件实体（`CatalystEvent`）**：
   - **标识与修订**：`event_id`、`category`（`MACRO | EARNINGS | CORPORATE_ACTION`）、关联 `instrument_id`（宏观事件关联市场标签）、`period_end`、`revision_id`（保留 ALFRED 历史 `vintage` 或财报更正追加记录，绝不覆写旧版）。
   - **预期排期侧**：`scheduled_date`、`scheduled_session`（`BMO | DMH | AMC | EXACT_UTC | DATE_ONLY`）、`scheduled_ts_utc`（仅当 BLS `.ics`、BEA JSON/ICS、FOMC 日历等官方源给出精确钟点如 `08:30 ET` / `14:00 ET` 时非空）、以及本排期获知入库的 `schedule_available_ts`（每日增量快照 Finnhub `/calendar/earnings` 与官方宏观日程表以积累长历史前瞻记录）。
   - **实际披露侧**：`actual_payload`（实际公布值、预期值快照、原文引用或 SEC Accession 号）、`ts_published`、`ts_available`、`precision_tier`（`SECOND_VERIFIED` | `SESSION_BOUND` | `DATE_CONSERVATIVE`）与 `ts_committed`。美股财报实际披露时刻由 **SEC EDGAR `submissions`（`8-K` Item `2.02` / `10-Q` / `10-K`）** 的 `acceptanceDateTime` 经美东时区（EST/EDT）与 Reg S-T `17:30 ET` 跨日规则校准后转为 UTC 秒级 `SECOND_VERIFIED` 时间戳（辅以 Alpaca News 秒级 `created_at`）；严禁爬虫抓取 `api.nasdaq.com`。
2. **历史与实时防前视裁剪法则**：
   - 在任意决策截止时刻 `cutoff_ts` 查询日历时，只要 `schedule_available_ts <= cutoff_ts`，允许下游看到“未来预期排期窗口”（供预警提示与组合风控层执行事件前降杠杆/停开新仓门禁）。
   - 但 `actual_payload`（公布结果）仅当 `cutoff_ts >= effective_available_ts`（`OBSERVED_REPLAY` 下还须 `>= ts_committed`）时方才解封可见，否则置为 `UNRELEASED`；其中 `effective_available_ts` 按精度等级执行硬保守折算：
     - `SECOND_VERIFIED`：直接采用已核验秒级 `ts_available`；
     - `SESSION_BOUND`（仅有 `BMO / AMC` 无秒级证据）：`BMO` 保守折算至当日美股常规开盘 `09:30:00 ET`（盘前时段不可偷看），`AMC` 保守折算至当日盘后结束 `20:00:00 ET`；
     - `DATE_CONSERVATIVE`（如仅有 `YYYY-MM-DD` 的 FRED/日级源）：一律推迟至下一日 `09:30:00 ET` 后可见，严禁把日级日期当作当日零点或盘中已知。

### 3. `Watchlist`、毫秒级 `Alert` 与 `Scanner` 状态及分发契约

1. **`Watchlist` 与订阅配额协调器**：
   - 存储在 `workspace_state.sqlite`，区分人工静态自选组（`MANUAL_GROUP`）与扫描器动态焦点池（`SCANNER_FOCUS`），按优先级（活跃持仓标的 > 挂有活跃预警标的 > 当前打开图表标的 > 普通自选）自动调度有限实时流配额（如 Alpaca 免费层 30 标的上限）。
2. **`AlertEngine` 与向量化 `Scanner` 状态模型**：
   - 统一消费热端闭合 K 线/BBO/衍生品增量与 L0 `operators` 纯函数算子，支持四类确定性预警条件：① 价格/盘口/费率/持仓量阈值（`METRIC_THRESHOLD`）；② 技术指标交叉或极值（`INDICATOR_SIGNAL`）；③ 图表画线触碰或收盘有效突破（`OVERLAY_CROSS`，按 `overlay_id` 实时读取同一感知层内 `OverlayRecord` 最新有效锚点做几何时间线性插值，人工改线即刻联动生效）；④ 催化剂临近倒计时（`CATALYST_COUNTDOWN`）。
   - 规则状态机：`ARMED` → 触发且满足冷却锁 → 立即调用 `freeze_snapshot` 生成带唯一 `snapshot_id` 的不可变 `TriggerEvent` → 单次预警转 `EXPIRED`、重复预警转 `COOLDOWN(until_ts)`（到期自动回 `ARMED`）；标的处于 `STALE` 断流态时挂起触发防止假信号。
3. **双轨分发路由契约（`TriggerRoutingPolicy`）**：
   - 每条 `AlertRule` 与 `ScannerRule` 显式配置两个正交动作标志：`notify_workbench: bool`（通过本地 WebSocket 毫秒级推送桌面弹窗/高亮闪烁与声音提醒，零大模型等待）与 `agent_wake_policy: Literal["NONE", "CO_PILOT_CONSULT", "AUTO_SUBACCOUNT"]`（指定唤醒哪个模式及目标 `sub_account_id`，由外圈编排器经过按标的冷却锁与全局最大并发会话令牌桶限流后异步投递给 DSH 桥接层，杜绝行情剧烈波动时引发模型调用风暴）。


## Comments

### 交接前整体复查修订（覆盖已解决 5、8 张）
- **原因**：独立复查指出四项跨模块衔接风险：① 实时热端内存环滚动淘汰或重启后历史快照复盘失联；② `ALPACA_IEX` 单场所仅 `08:00–17:00 ET` 供流且成交稀疏，易误触发断流降级；③ 美股 1 个月前历史缺乏免费事前财报排期 `schedule_available_ts`；④ `Scanner / AlertEngine` 在回测态的输入流接口需统一。
- **修订结论**：
  1. **热端冻结快照即时物化**：实时 `Shadow Run` 每次因触发器、会诊或开平仓调用 `freeze_snapshot` 时，自动将未归入冷库的热流切片落盘至 `state/hot_snapshots/<snapshot_id>.parquet` 并登记于 `runtime_snapshots.sqlite`，保障进程重启后永久可复现。
  2. **区分传输断流与供流窗口/稀疏无成交**：`FeedCapabilitySpec` 增加 `realtime_active_window`（如 `ALPACA_IEX` 为 `08:00–17:00 ET`）与传输层心跳超时；窗口内连接健康但无成交记为 `NO_TRADES`，仅传输失联或序列丢包记为 `FEED_GAP / STALE`。
  3. **历史财报排期覆盖度显式声明**：对无免费事前排期证据的美股早期历史回测区间，显式标记 `EarningsScheduleCoverage = EX_POST_ONLY`（严禁借事后 `8-K` 伪造事前静默门禁）并在评测报告头部披露，可选启用基于上年同季披露周的纯无前视保守宽窗规则。
  4. **统一 `MarketEventFeed` 接口**：`Scanner` 与 `AlertEngine` 面向统一事件流协议编程，实时态接热流、回测与寻优态接冷库迭代器，共享同一套触发与冷却状态机。
