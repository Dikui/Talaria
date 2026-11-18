# 加密永续公开衍生品流与美股 `Catalyst Calendar` 数据源能力边界：一手官方文档核查

- **核查日期**：`2026-11-18`（`Asia/Shanghai`）
- **定位**：在仓库既有核查（[trading-data-center-design.md](./trading-data-center-design.md)、[trading-data-templates-sources.md](./trading-data-templates-sources.md)）基础上，面向 Talaria 数据与感知层设计，专项查证两大前置选型问题：
  1. **主流加密永续场所（Binance USD-M Futures、Hyperliquid、Bybit V5 Linear）公开免密钥 API 的衍生品行情覆盖、真实历史回溯深度上限、分页/频控约束与官方批量归档能力**；
  2. **个人研究与模拟可用的美股日内行情、财报日历（Earnings Calendar）与宏观经济事件日历（Economic Calendar）公开/免费层 API 的可用性、时间戳精度（能否支撑日内防前视时点过滤）与使用条款限制**。
- **方法声明**：仅依据各平台官方开发者文档、官方 API 规范、官方公共数据仓库及官方服务条款/监管规则（如 SEC Regulation S-T）核查；**未注册收费套餐、未绕过站点访问控制、未发送实盘交易指令**。正文中严格区分**已核实官方事实**与面向 Talaria 的**工程推断**。

---

## 1. 核心结论（区分事实与推断）

### 1.1 已核实官方事实摘要

1. **加密永续三场所公开免鉴权能力高度不对称，尤其体现在「历史 K 线回溯深度」与「实时/历史 `Open Interest`（持仓量）」上**：
   - **Binance USD-M Futures**：免密钥 REST/WebSocket 覆盖分钟 K 线、BBO、`Mark Price` / `Index Price` 与历史 `Funding Rate`，但存在两项硬边界：① **官方公共 WebSocket 无实时 `Open Interest` 推送流**（仅能轮询 `GET /fapi/v1/openInterest`）[BN-ws][BN-oi]；② **REST 历史持仓量 `GET /futures/data/openInterestHist` 仅保留最近 1 个月（30 天）数据**[BN-oihist]。突破 30 天限制须使用官方免密钥归档站 `data.binance.vision` 的 `futures/um/daily/metrics`（5 分钟粒度持仓量与多空比），但该归档站的 USD-M `bookTicker` 历史文件已于 `2024-03-30` 后停止更新[BN-vision][BN-issue315]。
   - **Hyperliquid**：`POST /info` 与公共 WebSocket 完全免密钥，且 WebSocket `activeAssetCtx` 原生实时推送 `markPx`、`oraclePx`、`funding` 与 `openInterest`[HL-perp][HL-ws]；但 REST 历史回溯存在两堵硬墙：① **`candleSnapshot` 每个周期仅保留最近 `5,000` 根 K 线**（`1m` 约 3.47 天、`5m` 约 17.36 天）[HL-info]；② **`POST /info` 无任何历史 `Open Interest` 查询端点**[HL-perp]。突破上述限制须通过 AWS **Requester-Pays** 桶 `s3://hyperliquid-archive/asset_ctxs/`（按月上传 `.csv.lz4`）及 `s3://hl-mainnet-node-data/` 原始成交块自行聚合[HL-hist]。
   - **Bybit V5 Linear**：是三者中**唯一仅靠公开免密钥 REST + WebSocket 即可闭环全部五类实时与长历史衍生品指标**的场所——`GET /v5/market/kline`、`mark-price-kline`、`funding/history` 与 **`GET /v5/market/open-interest`（支持 `5min`–`1d` 粒度与 `cursor` 分页）均可回溯至合约上线日**，且公共 WebSocket 同时提供 `10ms` 级 `orderbook.1`（BBO）与 `100ms` 级 `tickers`（含 `markPrice`、`indexPrice`、`fundingRate`、`openInterest` 增量推送）[BB-kline][BB-oi][BB-ws-ob][BB-ws-ticker]。
2. **美股日内行情与 `Catalyst Calendar` 不存在「单一免费接口同时给出全市场前瞻排期 + 秒级实际公布时刻」的银弹**：
   - **美股日内行情（Alpaca Basic 免费层）**：提供 `200 req/min` REST 与自 `2016` 年起的历史分钟 K 线；但免费层实时流仅限 **IEX 单场所**（最多订阅 30 个标的），全市场 **SIP** 合并源在免费层有 **最近 15 分钟延迟/禁查限制**[ALP-plan][ALP-faq]。Alpaca 的 `/v2/calendar` 仅为交易所开休市日历，`/v2/corporate_actions/announcements` 仅含分红/拆股/并购，**均不含财报日历或宏观日历**；其 `/v1beta1/news` 提供带 RFC-3339 秒级 `created_at` / `updated_at` 的新闻流（自 2015 年起）[ALP-cal][ALP-ca][ALP-news]。
   - **美股财报日历（预期排期 vs 实际披露时刻）**：
     - **Finnhub 免费层**（`60 calls/min`，限个人非商业）开放 `/api/v1/calendar/earnings`，提供未来排期与**最近 1 个月历史**，时间精度为日期 `YYYY-MM-DD` 加时段枚举 `hour`（`"bmo"` 盘前 / `"amc"` 盘后 / `"dmh"` 盘中），**无精确到分秒的时间戳**[FH-earn][FH-price]。
     - **FMP（Financial Modeling Prep）免费层**（`250 calls/day`，限个人非商业）已停用旧版 `/api/v3/`；新版 `/stable/earnings-calendar` 在免费层**仅限约 87 个示例标的且历史上限 1 个月**，且官方已从该稳定版端点响应中**移除了 `bmo/amc` 时段字段，仅返回 `YYYY-MM-DD` 日期**[FMP-earn][FMP-price]。
     - **Nasdaq 官网日历（`api.nasdaq.com/api/calendar/earnings`）**属于未公开文档化的前端内部接口，Nasdaq 服务条款明文禁止未经书面许可的爬虫/自动化脚本抓取[NSDQ-tos]；官方合规渠道 **Nasdaq Data Link** 上的 Zacks / Wall Street Horizon 财报日历均为付费商业订阅[NSDQ-dl]。
     - **SEC EDGAR `data.sec.gov/submissions/`** 完全免费、免密钥（限 `10 req/s`，须声明可识别 `User-Agent`）[SEC-api][SEC-access]，**不提供未来财报排期**，但对已提交的 `8-K`（Item `2.02` 业绩披露）、`10-Q`、`10-K` 提供**秒级 `acceptanceDateTime`（实际受理公开时刻）**。需注意两项官方规则与陷阱：① 按 SEC Regulation S-T Rule 13，`17:30 ET` 后受理的 `8-K/10-Q/10-K` 其法定 `filingDate` 会顺延至下一交易日[SEC-regst]；② 原始 SGML 报文头 `<ACCEPTANCE-DATETIME>` 定义为美东时间（ET），而 JSON 接口中带 `"Z"` 后缀的 `acceptanceDateTime` 需与美东时间/原始报文头校验，不可盲目按纯 UTC 解析而不做核验[SEC-api][SEC-regst]。
   - **宏观经济事件日历（Economic Calendar）**：
     - **Finnhub `/api/v1/calendar/economic` 为付费专属（Premium Required），免费层不可用**[FH-econ]；**FMP `/stable/economic-calendar`** 虽返回 **UTC 秒级字符串（`YYYY-MM-DD HH:MM:SS`）**，但在免费层受限/门控（Starter 付费层起开放 1 年窗口）[FMP-econ][FMP-price]。
     - **FRED / ALFRED API**（免费 API Key，`120 req/min`）的 `fred/releases/dates?include_release_dates_with_no_data=true` 原生支持未来排期与历史发布日查询，并配合 `vintagedates` 提供历史修订版本，但**仅精确到日期 `YYYY-MM-DD`（无盘中时分秒）**[FRED-dates][FRED-vintage]。
     - **美国联邦统计与货币当局一手日程源**（公共领域免费）：**BLS**（`bls.gov/schedule/news_release/bls.ics`）、**BEA**（`bea.gov/news/schedule` 提供官方 JSON 与 ICS）及 **美联储 FOMC**（`federalreserve.gov/monetarypolicy/fomccalendars.htm`）直接发布权威预期日期与**固定美东钟点**（如 CPI/非农/PPI/GDP/PCE 为 `08:30 ET`，FOMC 声明为 `14:00 ET`）[US-bls][US-bea][US-fed]。

### 1.2 面向 Talaria 数据层的工程推断与架构启示

> **推断说明**：以下四条结论由上述已核实事实结合 Talaria 的时点纪律（`Event Time` / `Available Time` / `Committed Time`）推导得出，不属于第三方文档原话。

1. **`Market Pack` 能力特征（Capability Traits）必须细化到数据流级而非场所级**：不能假定“接入永续合约 = 自动拥有 REST 历史 OI 与长周期 1m K 线”。连接器协议需显式声明 `max_rest_kline_lookback_bars`、`has_rest_oi_history`、`has_ws_oi_stream` 与 `archive_backfill_driver`：
   - 若要求**零归档运维负担即可回测数月 `5m` OI + K 线 + 费率**，**Bybit V5 Linear** 是最省心的单接口基准源；
   - 若以 **Binance USD-M** 为主力源，必须实现「实时/近端走 `/fapi` REST+WS + 超过 30 天历史回填走 `data.binance.vision`（`klines` / `markPriceKlines` / `fundingRate` / `metrics`）」的双通道 Adapter，并单独用低频定时任务轮询 `GET /fapi/v1/openInterest` 补齐实时 OI；
   - 若接入 **Hyperliquid**，实时 `Shadow Run` 必须持续本地落盘 `candle` 与 `activeAssetCtx` 流，否则一旦断流超过 3.47 天（`1m`）便无法用免费 REST 补齐缺口。
2. **`Catalyst Calendar` 必须采用「预期窗口模型 + 实际可用时间（`Available Time`）双字段」并携带精度等级（`Precision Tier`）**：
   - 预期侧记录 `expected_date` + `expected_session`（`BMO | DMH | AMC | EXACT_TIME | DATE_ONLY`）+ `expected_time_utc`（仅当官方源给出钟点时非空）；
   - 实际侧记录 `available_at_utc` + `precision_tier`（`SECOND_VERIFIED` | `SESSION_BOUND` | `DATE_CONSERVATIVE`）。
3. **美股财报日历推荐采用「Finnhub 免费层（近端排期）+ SEC EDGAR Submissions（全历史秒级落地验证）」组合，拒绝抓取 `api.nasdaq.com`**：
   - 盘前/实盘 `Shadow Run` 用 Finnhub `/calendar/earnings` 获取未来排期与 `bmo/amc` 标记并按日快照持久化；
   - 历史日内回测与防前视校验以 SEC EDGAR `8-K`（Item `2.02`）/ `10-Q` / `10-K` 的受理时刻（校准美东时区后转为 UTC）作为财报正文与核心指标的真实 `Available Time`；若某历史事件仅有 `BMO/AMC` 而无秒级证据，日内回放须按保守上界裁剪（`BMO` 仅在当日 `09:30 ET` 开盘后视为已知，`AMC` 仅在盘后结束/次日盘前视为已知；若仅有 `YYYY-MM-DD` 日期则须推迟至 `T+1` 盘前）。
4. **美股宏观日历推荐采用「FRED/ALFRED 发布日与修订库 + BLS/BEA/FOMC 官方机器可读日程表」双源拼装**：
   - 用 BLS `.ics`、BEA `JSON/.ics` 与美联储 FOMC 日历填充核心宏观事件（CPI、NFP、PPI、GDP、PCE、FOMC）的精确预期时间戳（`08:30:00 ET` / `14:00:00 ET`）；
   - 用 FRED/ALFRED 在事件窗口后拉取对应序列及历史 `vintage` 修订值，并在实时 `Shadow Run` 中以系统实际拉取入库的 `Committed Time` 作为智能体可读边界。

---

## 2. 问题一核查：三大加密永续场所公开免密钥 API 与归档能力对比

### 2.1 五类核心衍生品数据流覆盖矩阵（全部为免密钥 Public 接口）

| 维度 | Binance USD-M Futures | Hyperliquid Perpetuals | Bybit V5 Linear (USDT/USDC) |
|---|---|---|---|
| **REST 基础地址** | `https://fapi.binance.com` [BN-general] | `POST https://api.hyperliquid.xyz/info` [HL-info] | `https://api.bybit.com` [BB-kline] |
| **WebSocket 公共地址** | `wss://fstream.binance.com/market` 及 `/public` [BN-ws] | `wss://api.hyperliquid.xyz/ws` [HL-ws] | `wss://stream.bybit.com/v5/public/linear` [BB-ws-conn] |
| **是否需要 API Key** | **否**（`NONE` 安全级别端点与公共流均免鉴权）[BN-general] | **否**（`info` 查询与公共订阅均免钱包签名/密钥）[HL-info] | **否**（`/v5/market/*` 与 `public` 流均免鉴权）[BB-kline] |
| **① 分钟级 K 线（REST）** | `GET /fapi/v1/klines`；支持 `1m,3m,5m,15m,30m,1h..1M` [BN-klines] | `type: "candleSnapshot"`；支持 `1m,3m,5m,15m,30m,1h..1M` [HL-perp] | `GET /v5/market/kline`（`category=linear`）；支持 `1,3,5,15,30,60..M`（按时间倒序返回）[BB-kline] |
| **① 分钟级 K 线（WS）** | `<symbol>@kline_<interval>`（250ms 推送，`x: true` 表闭合）[BN-ws] | `subscription: {type: "candle", coin, interval}` [HL-ws] | `kline.{interval}.{symbol}`（1–60s 推送，`confirm: true` 表闭合）[BB-ws-kline] |
| **② 最优买卖盘口 BBO（REST）** | `GET /fapi/v1/ticker/bookTicker`（单标的或全市场当前快照）[BN-book] | `type: "l2Book"`（盘口快照）或 `type: "allMids"`（中间价快照）[HL-info] | `GET /v5/market/tickers`（含 `bid1Price/Size`、`ask1Price/Size`）或 `/v5/market/orderbook?limit=1` [BB-tickers] |
| **② 最优买卖盘口 BBO（WS）** | `<symbol>@bookTicker` 或全市场 `!bookTicker`（实时逐笔最优价推送）[BN-ws] | `subscription: {type: "bbo", coin}`（区块内 BBO 变动时推送）[HL-ws] | `orderbook.1.{symbol}`（**10ms** 级 1 档推送）及 `tickers.{symbol}`（100ms 增量）[BB-ws-ob][BB-ws-ticker] |
| **③ `Mark Price` / `Index Price`** | **REST 实时**：`GET /fapi/v1/premiumIndex`<br>**REST 历史 K 线**：`GET /fapi/v1/markPriceKlines`、`indexPriceKlines`<br>**WS**：`<symbol>@markPrice`（3s）或 `@1s`（1s）、`!markPrice@arr` [BN-mark][BN-mark-klines][BN-ws] | **REST 实时**：`type: "metaAndAssetCtxs"`（含 `markPx`、`oraclePx`、`midPx`）<br>**REST 历史 K 线**：**无独立端点**<br>**WS**：`activeAssetCtx`（实时推送 `markPx`、`oraclePx`）[HL-perp][HL-ws] | **REST 实时**：`GET /v5/market/tickers`<br>**REST 历史 K 线**：`GET /v5/market/mark-price-kline`、`index-price-kline`<br>**WS**：`tickers.{symbol}`（100ms `snapshot` + `delta`，未变字段省略）[BB-mark-kline][BB-ws-ticker] |
| **④ 实时与历史 `Funding Rate`** | **实时**：`premiumIndex` + `fundingInfo` + WS `markPrice`<br>**历史结算**：`GET /fapi/v1/fundingRate` [BN-mark][BN-funding] | **实时（每小时结算）**：`metaAndAssetCtxs` + WS `activeAssetCtx`<br>**历史结算**：`type: "fundingHistory"`（必填毫秒 `startTime`）[HL-perp][HL-ws] | **实时**：`tickers`（含 `fundingRate`、`nextFundingTime`、`fundingIntervalHour`）+ WS `tickers`<br>**历史结算**：`GET /v5/market/funding/history` [BB-tickers][BB-funding] |
| **⑤ 实时与历史 `Open Interest`** | **REST 实时**：`GET /fapi/v1/openInterest`<br>**WS 实时**：**不支持（无原生 OI 流）**<br>**REST 历史**：`GET /futures/data/openInterestHist`（`5m..1d`）[BN-oi][BN-oihist][BN-ws] | **REST 实时**：`metaAndAssetCtxs`（`openInterest`，双边汇总口径）<br>**WS 实时**：**支持**（`activeAssetCtx`）<br>**REST 历史**：**不支持（无历史 OI 端点）**[HL-perp][HL-ws] | **REST 实时**：`GET /v5/market/tickers`（`openInterest`、`openInterestValue`）<br>**WS 实时**：**支持**（`tickers.{symbol}` 100ms 增量）<br>**REST 历史**：`GET /v5/market/open-interest`（`5min..1d`）[BB-oi][BB-ws-ticker] |

### 2.2 历史回溯时间深度上限、单次分页条数与公开频控核查表

| 场所与指标 | 真实 REST 回溯深度上限（已核实） | 单次分页上限与翻页机制 | 公开频控（Rate Limit / Weight） | 官方批量归档补齐方案（突破 REST 限制） |
|---|---|---|---|---|
| **Binance USD-M**<br>历史 K 线 / Mark K 线 | **可回溯至合约上线**；但单次请求 `endTime - startTime` **不得超过 200 天**（超长区间须按 ≤200 天切片循环翻页）[BN-klines] | `klines` 默认 `500`、最大 **`1500`**（`markPriceKlines` 最大 `1500`/`1000`）；按 `startTime`/`endTime` 翻页 [BN-klines][BN-mark-klines] | 全局 IP 权重上限 **`2,400 weight / min`**（超限返回 `HTTP 429`，持续违规返回 `HTTP 418` 封禁 IP 2 分钟–3 天）；K 线单次权重按 `limit` 分档：`[1,100)->1`、`[100,500)->2`、`[500,1000]->5`、`>1000->10` [BN-general][BN-klines] | **`https://data.binance.vision/data/futures/um/`**（免密钥、带 `.CHECKSUM` SHA-256 校验）：提供 `daily` 与 `monthly` 的 `klines`、`markPriceKlines`、`indexPriceKlines` 全历史 ZIP/CSV [BN-vision] |
| **Binance USD-M**<br>历史 `Funding Rate` | **可回溯至合约上线**（不受 30 天滚动窗口限制）[BN-funding] | 默认 `100`（部分文档标注 500），最大 **`1000`**；按 `startTime`/`endTime` 翻页 [BN-funding] | 与 `/fapi/v1/fundingInfo` **共享 `500 requests / 5 min / IP` 独立限流池** [BN-funding] | `data.binance.vision` 提供 `futures/um/monthly/fundingRate/{symbol}/` 按月归档文件 [BN-vision] |
| **Binance USD-M**<br>历史 `Open Interest` | **硬上限：仅保留最近 1 个月（30 天）**（`/futures/data/openInterestHist`）[BN-oihist] | 粒度 `5m,15m,30m,1h,2h,4h,6h,12h,1d`；默认 `30`，最大 **`500`** [BN-oihist] | IP 权重记为 `0`，但受 **`1,000 requests / 5 min / IP`** 独立限流约束 [BN-oihist] | **`data.binance.vision` 提供 `futures/um/daily/metrics/{symbol}/`**：按 **5 分钟**网格记录 `sum_open_interest`、`sum_open_interest_value` 与多空比，**可突破 REST 30 天限制**。*注：同目录 USD-M `bookTicker` 归档已于 `2024-03-30` 后停更* [BN-vision][BN-issue315] |
| **Binance USD-M**<br>公共 WebSocket 约束 | 仅实时流，无历史回放 | 单连接最多订阅 **`1,024` 个 stream**；单连接最长存活 **24 小时** [BN-ws] | 每 IP 每 5 分钟最多 `300` 次连接尝试；控制消息上限约 `5–10` 条/秒，须响应 `ping/pong` [BN-ws] | — |
| **Hyperliquid**<br>历史 K 线（`candleSnapshot`） | **硬上限：每个周期仅保留最近 `5,000` 根 K 线**[HL-info]<br>*(算术折算：`1m` ≈ **3.47 天**，`5m` ≈ **17.36 天**，`15m` ≈ **52.08 天**，`1h` ≈ **208.3 天**)* | 时间区间查询单次通常最多返回 **`500`–`5000` 条**（通用时间范围分页规则为每次最多 500 元素，用末条时间戳推进 `startTime`）[HL-info][HL-perp] | 全局共享 **`1,200 weight / min / IP`**；`candleSnapshot` 基础权重 `20`，另按**每返回 60 条记录额外 +1 weight** 计费（`l2Book`/`allMids` 权重为 `2`）[HL-limits] | **S3 无现成 K 线文件**：官方提供 AWS **Requester-Pays** 桶 `s3://hl-mainnet-node-data`（`node_fills_by_block` 等原始成交数据）与 `s3://hyperliquid-archive/market_data`（L2 快照），需调用方承担 AWS 流量费并自行聚合成 K 线 [HL-hist] |
| **Hyperliquid**<br>历史 `Funding Rate` | **可回溯至网络/资产上线**（`fundingHistory`）[HL-perp] | 单次最多返回 **`500` 条**记录；将末条时间戳作为下一次 `startTime` 向后翻页 [HL-info][HL-perp] | 基础权重 `20`，另按**每返回 20 条记录额外 +1 weight** 计费 [HL-limits] | `s3://hyperliquid-archive/asset_ctxs/[date].csv.lz4` 中亦包含历史 `funding` 状态记录 [HL-hist] |
| **Hyperliquid**<br>历史 `Open Interest` | **REST 不提供（`0` 天）**：`POST /info` 仅有当前快照，无历史 OI 查询端点 [HL-perp] | 不适用 | `metaAndAssetCtxs` 当前快照每次权重 `20` [HL-limits] | **`s3://hyperliquid-archive/asset_ctxs/[date].csv.lz4`**（Requester-Pays，约**每月上传一次**）：包含历史 `openInterest`、`markPx`、`oraclePx` 快照，但无法覆盖尚未归档的当月近端缺口 [HL-hist] |
| **Hyperliquid**<br>公共 WebSocket 约束 | 仅实时流 | 每 IP 最多 **`10` 个并发 WS 连接**、共 **`1,000` 个订阅** [HL-limits] | 每分钟最多新建 `30` 个连接；全部连接合计发送消息上限 `2,000` 条/分钟、并发 `post` 请求上限 `100` [HL-limits] | — |
| **Bybit V5 Linear**<br>历史 K 线 / Mark K 线 | **可回溯至合约上线**（无 30 天或 5000 根硬截断）[BB-kline][BB-mark-kline] | `limit` 默认 `200`，最大 **`1000`**；按毫秒 `start` / `end` 翻页（响应按时间倒序排列）[BB-kline] | REST 全局公共限流为 **`600 requests / 5 seconds / IP`**（超限返回 `HTTP 403` 或错误码 `10006`）[BB-rate] | 官方归档站 **`https://public.bybit.com/`** 提供免密钥 `.csv.gz` 逐笔成交（`trading/`）、指数（`spot_index/`、`premium_index/`）与 K 线文件 [BB-pub] |
| **Bybit V5 Linear**<br>历史 `Funding Rate` | **可回溯至合约上线**（`GET /v5/market/funding/history`）[BB-funding] | `limit` 默认 `200`，最大 **`200`**；按 `startTime` / `endTime` 翻页（注意同时传 `startTime` 或 `endTime` 的官方校验约束）[BB-funding] | 计入每 IP `600 requests / 5 seconds` 公共配额 [BB-rate] | `public.bybit.com` 提供 `premium_index/`，结算费率可直接由 V5 REST 全量分页拉取 [BB-funding][BB-pub] |
| **Bybit V5 Linear**<br>历史 `Open Interest` | **可回溯至合约上线**（`GET /v5/market/open-interest`，官方明确上界为标的发布时刻）[BB-oi] | 支持 `intervalTime`：`5min, 15min, 30min, 1h, 4h, 1d`；`limit` 默认 `50`、最大 **`200`**；通过响应 `nextPageCursor` 传入 `cursor` 翻页 [BB-oi] | 计入每 IP `600 requests / 5 seconds` 公共配额 [BB-rate] | `public.bybit.com` 无独立 5m OI 压缩包，但 V5 REST 已原生支持回溯至上线日 [BB-oi][BB-pub] |
| **Bybit V5 Linear**<br>公共 WebSocket 约束 | 仅实时流（不消耗 REST 限流配额）[BB-rate] | 单一市场类别每 IP 最多 **`1,000` 个活跃行情连接** [BB-rate] | 每 IP 每 5 分钟最多建立 `500` 次连接；建议每 20s 发送心跳 [BB-rate][BB-ws-conn] | — |

### 2.3 加密场所使用条款与合规边界（已核实事实）

- **数据使用与再分发限制**：Binance 使用条款将公开 API 与归档数据限定于个人非商业使用或内部业务自用，未经书面授权禁止向第三方再分发原始行情流或以 Binance 名义对外展示报价[BN-terms]；Bybit API 条款同样禁止未经商业/经纪商授权的大规模对外商业转售行情[BB-terms]。
- **司法管辖区限制**：Hyperliquid 官方条款将美国（United States）、加拿大安大略省及受制裁地区列为受限司法管辖区（Restricted Jurisdictions），禁止受限地区人员使用其前端与应用层服务[HL-terms]；Binance 国际站（`fapi.binance.com`）与 Bybit 主站同样对美国等受限地区 IP 实施地理围栏。个人本地部署需确保运行网络符合所在地合规要求。

---

## 3. 问题二核查：美股日内行情与 `Catalyst Calendar`（财报/宏观日历）公开与免费层 API

### 3.1 候选官方源能力、时间戳精度与条款核查总表

| 数据源与官方端点 | 覆盖领域 | 免费层是否可用 | 时间戳字段与真实精度（防前视关键指标） | 免费层回溯/前瞻窗口与频控限制 | 使用条款与合规红线 |
|---|---|---|---|---|---|
| **Alpaca Market Data & Trading API**<br>`GET /v2/stocks/bars`<br>`wss://stream.data.alpaca.markets/v2/iex`<br>`GET /v2/calendar`<br>`GET /v2/corporate_actions/announcements`<br>`GET /v1beta1/news` | 美股日内行情、交易时段日历、公司行动、实时金融新闻 | **可用**（`Basic` 计划 \$0/月，需免费注册 API Key）[ALP-plan] | - **K 线/逐笔**：UTC RFC-3339 秒/纳秒级；注意实时流存在迟到成交修正 `updatedBars`，且历史 `asof` 仅处理代码更名而非修订时点 [ALP-bars][ALP-stream]<br>- **交易日历 `/v2/calendar`**：日期 + 常规/盘前盘后开休市分钟时刻（`1970–2029`），**不含财报或宏观事件** [ALP-cal]<br>- **新闻 `/v1beta1/news`**：**秒级 RFC-3339 UTC**（`created_at` 与 `updated_at` 分离）[ALP-news] | - **历史 K 线**：回溯至 **`2016` 年**；免费层查全市场 `SIP` 历史须距当前 **≥15 分钟**（最近 15 分钟仅能查 `IEX`）[ALP-plan][ALP-faq]<br>- **实时 WS**：仅 `IEX` 单场所，最多 **30 个标的** [ALP-plan]<br>- **新闻**：回溯至 `2015` 年，单页最多 50 条 [ALP-news]<br>- **频控**：**`200 requests / min`** [ALP-plan] | 严格限定 **Non-Professional（非专业个人自用）**，**严禁向第三方再分发**任何 IEX/SIP 行情与新闻内容 [ALP-terms] |
| **SEC EDGAR Public APIs**<br>`https://data.sec.gov/submissions/CIK{10d}.json`<br>及夜间全量包 `submissions.zip` | 美股上市公司法定披露（`8-K` Item `2.02` 业绩快报、`10-Q`、`10-K`）实际落地时间 | **完全免费且免密钥**（必须携带合规 `User-Agent: Name Email` 请求头）[SEC-api][SEC-access] | - **不提供未来预期财报日历**<br>- **实际披露时刻**：提供**秒级 `acceptanceDateTime`**、法定 `filingDate`（`YYYY-MM-DD`）与期末 `reportDate`<br>- **两大时点陷阱（已核实）**：① 按 17 CFR § 232.13（Reg S-T Rule 13），`17:30 ET` 后受理的 `8-K/10-Q/10-K` 其 `filingDate` 会记作**下一营业日**；② 原始 SGML `<ACCEPTANCE-DATETIME>` 为**美东时间（ET）**，JSON 字符串虽带 `"Z"` 后缀，工程解析必须与美东时区/SGML 头交叉校验，不可盲信字面 UTC [SEC-api][SEC-regst] | - **覆盖范围**：全美 SEC 申报实体全量历史<br>- **频控**：全站上限 **`10 requests / second / IP`**；大批量历史初始化官方要求优先下载夜间批量包 `submissions.zip` / `companyfacts.zip` [SEC-api][SEC-access] | 美国联邦政府公共领域数据（Public Domain），允许个人与商业研究使用，唯须严格遵守公平访问速率与 `User-Agent` 规范 [SEC-access] |
| **Finnhub API**<br>`GET /api/v1/calendar/earnings`<br>`GET /api/v1/calendar/economic` | 美股财报日历（免费可用）；全球宏观日历（**付费专属**） | - **财报日历：免费层可用**（\$0/月）[FH-price]<br>- **宏观日历：免费层不可用（Premium Required）**[FH-econ] | - **财报日历**：返回 `date`（`YYYY-MM-DD`）+ **`hour` 时段标记**（`"bmo"` 盘前、`"amc"` 盘后、`"dmh"` 盘中或空串），含预期与实际 EPS/营收，但**无精确到分秒的预期或实际公布时间戳** [FH-earn]<br>- *(付费版宏观日历含 `time` 字段，但不在免费范围内)* [FH-econ] | - **财报日历免费回溯**：美股未来排期 + **仅最近 1 个月历史数据**（20 年以上长历史需付费套餐）[FH-price]<br>- **频控**：免费层 **`60 API calls / min`** [FH-price] | 免费层受 **Personal Use（个人非商业使用）** 条款约束，禁止商业用途与对外再分发 [FH-price] |
| **Financial Modeling Prep (FMP)**<br>`GET /stable/earnings-calendar`<br>`GET /stable/economic-calendar` | 财报日历、全球宏观经济数据发布日历 | **高度受限**：免费层（\$0/月）已废弃旧 `/api/v3/`；稳定版财报日历仅开放沙盒级子集，宏观日历在免费层门控受限 [FMP-price][FMP-econ] | - **财报日历（`/stable/earnings-calendar`）**：**仅返回日期 `date`（`YYYY-MM-DD`）与 `lastUpdated`（`YYYY-MM-DD`）**——官方稳定版已**移除 `bmo/amc` 时段字段**，无法区分当日盘前或盘后发布！[FMP-earn]<br>- **宏观日历（`/stable/economic-calendar`）**：返回 **`date`（`YYYY-MM-DD HH:MM:SS`，官方明确为 UTC 时区）**，含预期/前值/实际值与影响等级 [FMP-econ] | - **免费层配额**：总共 **`250 calls / day`**（及 30 天 500MB 带宽上限）[FMP-price]<br>- **免费层财报覆盖**：仅限**约 87 个示例标的**且历史回溯最多 **1 个月**（全美股 + 1 年回溯需 Starter \$29/月；单次请求窗口上限 90 天）[FMP-price][FMP-earn]<br>- **宏观日历**：Starter（\$29/月）起提供 1 年窗口，完整历史需 Premium/Ultimate [FMP-price] | 免费与个人套餐严格限定**个人非商业用途**，禁止再分发或商业产品集成 [FMP-price] |
| **FRED / ALFRED API**<br>`GET /fred/releases/dates`<br>`GET /fred/release/dates`<br>`GET /fred/series/vintagedates` | 宏观经济数据发布日历与历史时点修订（Vintage）数据库 | **可用**（免费申请 32 位 API Key）[FRED-dates] | - **仅日期精度（`YYYY-MM-DD`）**：无论 `date` 还是开启 `include_release_dates_with_no_data=true` 后返回的 `release_last_updated`，**均只精确到日历日，不含盘中发布钟点（`HH:MM:SS`）**[FRED-dates]<br>- ALFRED `realtime_start`/`realtime_end` 与 `vintagedates` 同样为日级 `YYYY-MM-DD` [FRED-vintage] | - **前瞻与回溯**：`include_release_dates_with_no_data=true` 可同时返回全历史发布日与数据源已排定的**未来发布日期**；单页 `limit` 最大 `1000` [FRED-dates]<br>- **频控**：每 API Key **`120 requests / min`**（超限返回 `HTTP 429`）[FRED-dates] | 联邦公共领域序列可自由用于研究；但 FRED 托管的部分第三方版权序列保留原权利人限制，不可擅自再分发或违规转交外部商业服务 [FRED-terms] |
| **美国官方统计与货币机构一手日程**<br>- BLS：`bls.gov/schedule/news_release/bls.ics`<br>- BEA：`bea.gov/news/schedule`（JSON / ICS）<br>- Fed：`federalreserve.gov/monetarypolicy/fomccalendars.htm` | 核心美股宏观催化剂排期（CPI、非农就业 NFP、PPI、GDP、PCE、FOMC 利率决议） | **完全免费、免密钥**（官方公开机器可读日程表）[US-bls][US-bea][US-fed] | **精确到分钟/秒级预期发布时刻（美东时间 ET）**：<br>- **BLS** `.ics` 日历：CPI、Employment Situation（非农）、PPI 等明确标注 **`08:30 AM ET`** [US-bls]<br>- **BEA** 官方 JSON / ICS：GDP、Personal Income & Outlays（PCE）明确标注 **`08:30 AM ET`** [US-bea]<br>- **美联储 FOMC**：议息声明与经济预测（SEP）常规固定于会议次日 **`2:00 p.m. ET`**（发布会 `2:30 p.m. ET`）[US-fed] | 覆盖当年及已排定的下一年度全部法定发布窗口；静态日历文件低频同步（如每日/每周一次）即可，几乎无频控压力（访问 `bls.gov` 须遵守文明速率与可读 `User-Agent`）[US-bls][US-bea] | 美国联邦政府一级来源（Public Domain），无商业版权障碍，是校准 FRED 日级日历盘中时刻的权威基准 [US-bls][US-bea][US-fed] |
| **Nasdaq 官网日历 vs Nasdaq Data Link**<br>`api.nasdaq.com/api/calendar/*` vs `data.nasdaq.com` | 美股财报日历与经济日历 | **无免费合规自动化 API**：官网内部接口禁爬；Data Link 对应数据集为付费订阅 [NSDQ-tos][NSDQ-dl] | - 官网页面展示盘前/盘后图标，但非受支持的开发者契约接口<br>- Data Link 机构级表（如 *Zacks Earnings Announcements*、*Wall Street Horizon*）具备修订级公司事件跟踪能力，但属商业付费产品 [NSDQ-dl] | 官网前端 JSON 接口受 WAF 频控与反爬拦截，随时无通知变更字段 [NSDQ-tos] | **合规否决项**：`nasdaq.com` 服务条款明文禁止使用任何 robot、spider、scraper 或自动化程序抓取站点及底层接口数据 [NSDQ-tos] |

---

### 3.2 日内防前视过滤（Intraday Point-in-Time）关键边界剖析

结合仓库既有双时间设计（`Event Time` vs `Available Time` vs `Committed Time`），本次一手核查揭示了美股日内场景下三个极易导致前视泄漏（Look-Ahead Bias）的真实接口细节：

1. **为什么日级财报日历（如 FMP `/stable/earnings-calendar`）不能直接用于日内回测**：
   - **已核实事实**：美股绝大多数公司选择盘前（`06:00–09:15 ET`）或盘后（`16:01–17:00 ET`）发财报；FMP 稳定版财报日历仅返回 `"date": "2026-05-02"` 而无 `bmo/amc`[FMP-earn]；即便 Finnhub 返回 `"hour": "amc"`[FH-earn]，也未区分是 `16:02 ET` 还是 `16:45 ET` 发布。
   - **工程推断**：在盘中分钟级（如 `09:30–16:00 ET`）或盘后时段回测中，若把 `date = T` 的盘后财报当作 `T 09:30 ET` 可见，会产生整整一个常规交易日的严重前视偏差；而在盘后交易（`16:00–20:00 ET`）回测中，连 `"amc"` 标签也不足以判断 `16:05 ET` 的 K 线是否已知该财报，**必须依赖 SEC EDGAR `8-K`（Item `2.02`）的秒级受理时刻或 Alpaca News 的秒级 `created_at` 作为 `Available Time` 证据**。
2. **为什么 SEC EDGAR 不能只读 `filingDate`，且必须校验 `acceptanceDateTime` 时区**：
   - **已核实事实**：根据 17 CFR § 232.13（Regulation S-T Rule 13），若一家公司在周二盘后 `17:40 ET` 向 EDGAR 提交业绩 `8-K` 并获受理，其精确受理时刻为周二 `17:40 ET`，但法定 `filingDate` 会被计入**周三**；反之，若在周二 `16:05 ET` 提交，`filingDate` 为周二当天但无时分秒[SEC-regst]。同时 EDGAR 内部标准时钟与原始 SGML `<ACCEPTANCE-DATETIME>` 均为美东时间（ET）[SEC-regst]。
   - **工程推断**：数据接入层解析 `data.sec.gov/submissions/` 时，必须提取 `acceptanceDateTime` 并通过原始报文头抽样校验美东时区转换规则（EST `UTC-5` / EDT `UTC-4`），将其统一归一化为真实 UTC 秒级时间戳写入 `source_available_at`，绝不能用 `filingDate 00:00:00` 代替。
3. **为什么宏观日历不能单靠 FRED API 实现盘中预警与停手门禁（Event Halt Gate）**：
   - **已核实事实**：FRED `releases/dates` 只给出 `YYYY-MM-DD`[FRED-dates]；而加密永续（24×7 连续交易）与美股盘前时段在 CPI / 非农（`08:30:00 ET`）及 FOMC（`14:00:00 ET`）公布瞬间会发生剧烈波动与点差扩张[US-bls][US-fed]。
   - **工程推断**：Talaria 的 `Catalyst Calendar` 必须把 **BLS `.ics` + BEA `JSON/.ics` + FOMC 日历** 固化的一手钟点映射表与 **FRED `release_id`** 绑定，才能在 `08:30:00 ET` 前后精确触发盘面预警（`Alert`）或风控降档窗口；而在回放宏观实际数值时，若无秒级新闻电文时间戳，只能以官方计划发布时刻加上保守延迟偏移（Historical PIT）或本系统实际抓取入库的 `Committed Time`（Observed Replay）为准。

---

## 4. 面向 Talaria 第一阶段的数据源选型建议（工程推断）

| 业务模块与数据子域 | 首选公开/免费层方案（个人研究与 `Shadow Run`） | 备选 / 增强补齐方案 | 明确排除的作法 |
|---|---|---|---|
| **加密永续实时流（`HotStream`）**<br>（1m K 线、BBO、`Mark/Index`、实时费率与实时 OI） | **Bybit V5 Linear**（单公共 WS 同时拿齐 `kline`、`10ms` BBO、`markPrice`、`fundingRate`、实时 `openInterest`）+ **Binance USD-M**（公共 WS + 低频轮询 `/fapi/v1/openInterest`） | **Hyperliquid** 公共 WS（`candle` + `bbo` + `activeAssetCtx` 实时落盘自建本地历史库） | 假设 Binance WS 会推送实时 OI；或不落盘直接依赖 Hyperliquid REST 回补超过 3.4 天的 `1m` K 线 |
| **加密永续历史回测库**<br>（数月至数年分钟 K 线、历史费率、5m 历史 OI） | **Binance `data.binance.vision` 批量归档**（`klines` + `markPriceKlines` + `fundingRate` + 5m `metrics` OI）+ 近 30 天 `/fapi` REST 增量缝合；或 **Bybit V5 REST** 全量分页回填 | Hyperliquid 按需接入 AWS Requester-Pays `s3://hyperliquid-archive/asset_ctxs` 与 `hl-mainnet-node-data` | 仅用 Binance `/futures/data/openInterestHist` 回测超过 30 天的 OI 策略；指望 `data.binance.vision` 提供 2024-03-30 之后的 USD-M `bookTicker` |
| **美股日内行情与交易时段** | **Alpaca Basic（免费层）**：历史拉取 `≥15m` 前的 `SIP` 分钟 K 线（2016 起），实时 `Shadow Run` 接入 `IEX` WS（≤30 核心观察池标的）+ `/v2/calendar` 交易时段 | 需要全市场无延迟实时盘口与 >30 标的并发时，按合规非专业订阅升级 Alpaca *Algo Trader Plus* | 在免费层混用实时 `IEX` 与历史 `SIP` 却不记录 `feed` 身份标签 |
| **美股财报 `Catalyst Calendar`** | **前瞻排期**：**Finnhub 免费层** `/calendar/earnings`（含 `bmo/amc/dmh`，每日增量归档到本地 Parquet/SQLite 突破 1 个月滚动限制）<br>**实际披露 `Available Time`**：**SEC EDGAR `submissions`**（`8-K` Item `2.02` / `10-Q` / `10-K` 秒级受理时间）+ **Alpaca News** 秒级 `created_at` | 需全市场 >1 个月历史前瞻排期预期值（Consensus EPS）时再评估付费数据源 | 编写爬虫抓取 `api.nasdaq.com`（违反 Nasdaq ToS）；或把 FMP `/stable/earnings-calendar` 的无时段日期当作当日开盘已知事实 |
| **宏观经济 `Catalyst Calendar`** | **预期时刻表**：**BLS `.ics` + BEA `JSON/.ics` + 美联储 FOMC 官方日程**（精确到 `08:30 ET` / `14:00 ET`）<br>**发布序列与修订**：**FRED `releases/dates` + ALFRED `vintagedates`** | 付费升级 FMP Starter/Premium 获取全球多国 UTC 秒级 `/stable/economic-calendar` | 依赖免费层不可用的 Finnhub `/calendar/economic`；或把 FRED 的 `YYYY-MM-DD` 日期当作当日 `00:00 UTC` 已公布 |

---

## 5. 一手官方来源索引

### 5.1 加密永续交易场所官方文档与归档库

- **[BN-general]** Binance USDⓈ-M Futures General Info（基础域名、`2400/min` `REQUEST_WEIGHT`、`HTTP 429/418` 频控规则）：https://developers.binance.com/docs/derivatives/usds-margined-futures/general-info
- **[BN-klines]** Binance USDⓈ-M Kline/Candlestick Data（`GET /fapi/v1/klines`、`limit` 最大 `1500`、动态权重与 `startTime/endTime` 200 天窗口约束）：https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Kline-Candlestick-Data
- **[BN-mark-klines]** Binance USDⓈ-M Mark Price Kline/Candlestick Data（`GET /fapi/v1/markPriceKlines`）：https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Mark-Price-Kline-Candlestick-Data
- **[BN-mark]** Binance USDⓈ-M Mark Price（`GET /fapi/v1/premiumIndex`，实时标记价格、指数价格与当期资金费率）：https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Mark-Price
- **[BN-book]** Binance USDⓈ-M Symbol Order Book Ticker（`GET /fapi/v1/ticker/bookTicker`）：https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Symbol-Order-Book-Ticker
- **[BN-funding]** Binance USDⓈ-M Get Funding Rate History（`GET /fapi/v1/fundingRate`，最大 `1000` 条，与 `fundingInfo` 共享 `500 req / 5 min / IP`）：https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Get-Funding-Rate-History
- **[BN-oi]** Binance USDⓈ-M Open Interest（`GET /fapi/v1/openInterest` 实时持仓量快照，权重 `1`）：https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Open-Interest
- **[BN-oihist]** Binance USDⓈ-M Open Interest Statistics（`GET /futures/data/openInterestHist`，仅保留最近 1 个月/30 天，最大 `500` 条，`1000 req / 5 min / IP`）：https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Open-Interest-Statistics
- **[BN-ws]** Binance USDⓈ-M WebSocket Market Streams（`wss://fstream.binance.com`、`bookTicker`、`markPrice`、`kline` 流定义与连接/订阅限制）：https://developers.binance.com/docs/derivatives/usds-margined-futures/websocket-market-streams
- **[BN-vision]** Binance Public Data 官方归档说明与分发站（`futures/um` 下 `klines`、`markPriceKlines`、`fundingRate`、5 分钟 `metrics` 及 SHA-256 校验）：https://github.com/binance/binance-public-data/blob/master/README.md 与 https://data.binance.vision/
- **[BN-issue315]** Binance Public Data 官方仓库关于 USD-M `bookTicker` 归档于 `2024-03-30` 后停更的记录：https://github.com/binance/binance-public-data/issues/315
- **[BN-terms]** Binance Terms of Use（API 与平台市场数据个人自用及禁止未授权再分发条款）：https://www.binance.com/en/terms
- **[HL-info]** Hyperliquid Info Endpoint 总览（`POST https://api.hyperliquid.xyz/info`、`candleSnapshot` 最近 `5,000` 根上限、`l2Book`/`allMids` 与时间范围 `500` 条分页说明）：https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint
- **[HL-perp]** Hyperliquid Perpetuals Info Endpoint（`metaAndAssetCtxs` 实时 `markPx`/`oraclePx`/`funding`/`openInterest`、`fundingHistory` 历史费率，无历史 OI 端点）：https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/perpetuals
- **[HL-ws]** Hyperliquid WebSocket Subscriptions（`wss://api.hyperliquid.xyz/ws`、`candle`、`bbo`、`l2Book`、`activeAssetCtx` 实时流）：https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/websocket/subscriptions
- **[HL-limits]** Hyperliquid Rate Limits and User Limits（`1,200 weight / min / IP`、按返回条数附加权重及 WebSocket 连接/订阅上限）：https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/rate-limits-and-user-limits
- **[HL-hist]** Hyperliquid Historical Data（AWS Requester-Pays 桶 `s3://hyperliquid-archive/asset_ctxs` 按月 `.csv.lz4` 与 `s3://hl-mainnet-node-data` 说明）：https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/historical-data
- **[HL-terms]** Hyperliquid Terms of Use（受限司法管辖区与接口使用规定）：https://app.hyperliquid.xyz/terms
- **[BB-rate]** Bybit V5 Rate Limit Rules（REST `600 req / 5s / IP` 与公共 WebSocket 连接速率/并发上限）：https://bybit-exchange.github.io/docs/v5/rate-limit
- **[BB-kline]** Bybit V5 Get Kline（`GET /v5/market/kline`，`limit` 最大 `1000`）：https://bybit-exchange.github.io/docs/v5/market/kline
- **[BB-mark-kline]** Bybit V5 Get Mark Price Kline & Index Price Kline（`GET /v5/market/mark-price-kline`、`index-price-kline`）：https://bybit-exchange.github.io/docs/v5/market/mark-kline
- **[BB-tickers]** Bybit V5 Get Tickers（`GET /v5/market/tickers` 实时 BBO、`markPrice`、`indexPrice`、`fundingRate`、`openInterest`）：https://bybit-exchange.github.io/docs/v5/market/tickers
- **[BB-funding]** Bybit V5 Get Funding Rate History（`GET /v5/market/funding/history`，`limit` 最大 `200`）：https://bybit-exchange.github.io/docs/v5/market/history-fund-rate
- **[BB-oi]** Bybit V5 Get Open Interest（`GET /v5/market/open-interest`，`5min..1d`、`limit` 最大 `200`、`cursor` 分页回溯至合约上线日）：https://bybit-exchange.github.io/docs/v5/market/open-interest
- **[BB-ws-conn]** Bybit V5 WebSocket Connect（`wss://stream.bybit.com/v5/public/linear`）：https://bybit-exchange.github.io/docs/v5/websocket/connect
- **[BB-ws-ob]** Bybit V5 WebSocket Public Orderbook（`orderbook.1.{symbol}` 10ms 级最优盘口推送）：https://bybit-exchange.github.io/docs/v5/websocket/public/orderbook
- **[BB-ws-ticker]** Bybit V5 WebSocket Public Ticker（`tickers.{symbol}` 100ms `snapshot` + `delta` 推送机制）：https://bybit-exchange.github.io/docs/v5/websocket/public/ticker
- **[BB-ws-kline]** Bybit V5 WebSocket Public Kline（`kline.{interval}.{symbol}` 与 `confirm` 闭合标志）：https://bybit-exchange.github.io/docs/v5/websocket/public/kline
- **[BB-pub]** Bybit 官方公开历史归档目录（`.csv.gz` 成交、K 线与指数历史）：https://public.bybit.com/
- **[BB-terms]** Bybit Terms & API Terms（API 安全与行情使用规范）：https://www.bybit.com/app/terms-service/information

### 5.2 美股日内行情、财报披露与宏观日历官方文档

- **[ALP-plan]** Alpaca Market Data Overview & Subscriptions（Basic 免费层 `200 req/min`、`IEX` 实时 30 标的上限、`SIP` 15 分钟延迟限制与 `2016` 历史深度）：https://docs.alpaca.markets/docs/about-market-data-api
- **[ALP-faq]** Alpaca Market Data FAQ（`IEX` 单场所与 `SIP` 全市场合并源差异）：https://docs.alpaca.markets/docs/market-data-faq
- **[ALP-bars]** Alpaca Historical Stock Bars（`GET /v2/stocks/bars`、`adjustment` 与 `asof` 代码映射语义）：https://docs.alpaca.markets/reference/stockbars
- **[ALP-stream]** Alpaca Real-Time Stock Pricing Data（WebSocket 流与 `updatedBars` 迟到修正机制）：https://docs.alpaca.markets/docs/real-time-stock-pricing-data
- **[ALP-cal]** Alpaca Market Calendar API（`GET /v2/calendar` 交易日开休市日历）：https://docs.alpaca.markets/reference/getcalendar-1
- **[ALP-ca]** Alpaca Corporate Actions Announcements（`GET /v2/corporate_actions/announcements` 分红/拆股/并购）：https://docs.alpaca.markets/reference/getcorporateactionsannouncements
- **[ALP-news]** Alpaca News API（`GET /v1beta1/news` 与 WebSocket，RFC-3339 秒级 `created_at` / `updated_at`）：https://docs.alpaca.markets/docs/streaming-real-time-news
- **[ALP-terms]** Alpaca Disclosures & Market Data Agreements（Non-Professional 个人自用及禁止再分发条款）：https://alpaca.markets/disclosures
- **[SEC-api]** SEC EDGAR Application Programming Interfaces（`data.sec.gov/submissions/CIK##########.json`、`acceptanceDateTime`、`8-K` `items` 与夜间 `submissions.zip` 批量包）：https://www.sec.gov/search-filings/edgar-application-programming-interfaces
- **[SEC-access]** SEC Accessing EDGAR Data（Fair Access `10 requests/second` 频控与必填 `User-Agent` 声明规范）：https://www.sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data
- **[SEC-regst]** SEC Regulation S-T Rule 13 — Date of Filing（17 CFR § 232.13：美东时间标准与 `17:30 ET` / `22:00 ET` 法定 `filingDate` 截断顺延规则）：https://www.ecfr.gov/current/title-17/chapter-II/part-232/section-232.13
- **[FH-earn]** Finnhub Earnings Calendar API（`GET /api/v1/calendar/earnings`、`date` + `hour: bmo/amc/dmh` 字段定义）：https://finnhub.io/docs/api/earnings-calendar
- **[FH-econ]** Finnhub Economic Calendar API（`GET /api/v1/calendar/economic`，标注 **Premium Access Required**）：https://finnhub.io/docs/api/economic-calendar
- **[FH-price]** Finnhub Pricing & Free Tier Limits（\$0 免费层 `60 calls/min`、美股财报日历 1 个月历史上限及 Personal Use 限制）：https://finnhub.io/pricing
- **[FMP-earn]** Financial Modeling Prep Stable Earnings Calendar API（`GET /stable/earnings-calendar` 响应仅含 `date` 与 `lastUpdated` 日期、单次最多 90 天窗口）：https://site.financialmodelingprep.com/developer/docs/stable/earnings-calendar
- **[FMP-econ]** Financial Modeling Prep Stable Economic Calendar API（`GET /stable/economic-calendar`，UTC `YYYY-MM-DD HH:MM:SS` 格式）：https://site.financialmodelingprep.com/developer/docs/stable/economics-calendar
- **[FMP-price]** Financial Modeling Prep Pricing Plans & Terms（免费层 `250 calls/day`、停用 Legacy 接口、免费层示例标的/历史深度限制及个人非商业条款）：https://site.financialmodelingprep.com/pricing-plans
- **[FRED-dates]** St. Louis Fed FRED API `fred/releases/dates` & `fred/release/dates`（`include_release_dates_with_no_data=true`、`YYYY-MM-DD` 日级精度与 `120 req/min` 限流）：https://fred.stlouisfed.org/docs/api/fred/releases_dates.html 与 https://fred.stlouisfed.org/docs/api/fred/release_dates.html
- **[FRED-vintage]** St. Louis Fed FRED API Realtime Period & Vintage Dates（历史时点修订查询机制）：https://fred.stlouisfed.org/docs/api/fred/realtime_period.html 与 https://fred.stlouisfed.org/docs/api/fred/series_vintagedates.html
- **[FRED-terms]** St. Louis Fed FRED API Terms of Use（API Key 规范与第三方版权数据序列限制）：https://fred.stlouisfed.org/docs/api/terms_of_use.html
- **[US-bls]** U.S. Bureau of Labor Statistics Schedule of Selected Releases & iCalendar Feed（CPI、Employment Situation、PPI 官方 `08:30 AM ET` 发布日程）：https://www.bls.gov/schedule/ 与 https://www.bls.gov/schedule/news_release/bls.ics
- **[US-bea]** U.S. Bureau of Economic Analysis News Release Schedule（GDP、PCE 官方 `08:30 AM ET` 发布日程及 JSON / ICS 下载入口）：https://www.bea.gov/news/schedule
- **[US-fed]** Board of Governors of the Federal Reserve System — FOMC Calendars & Statistical Release Schedule（FOMC 议息会议及 `2:00 p.m. ET` 声明发布规则）：https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm 与 https://www.federalreserve.gov/data/releaseschedule.htm
- **[NSDQ-tos]** Nasdaq.com Terms of Service（明文禁止未授权自动化爬虫/脚本抓取官网与前端接口数据）：https://www.nasdaq.com/terms-of-service
- **[NSDQ-dl]** Nasdaq Data Link Documentation（官方金融数据 API 平台，机构级财报/公司事件数据集需付费订阅）：https://docs.data.nasdaq.com/
