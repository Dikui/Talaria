# 核查加密永续公开衍生品流与美股宏观事件日历 API 能力边界

Labels: wayfinder:research
Type: research
Status: resolved
Blocked by: none

## Question

为支撑数据层的加密永续合约与美股日内双市场接入及 `Catalyst Calendar` 事件日历，需查证一手官方文档与 API 规范：
1. 主流加密永续场所（Binance USD-M、Hyperliquid、Bybit）公开免密钥 REST/WebSocket 接口在分钟 K 线、最优买卖盘口（BBO）、`Mark Price`、历史与实时 `Funding Rate` 以及历史 `Open Interest`（持仓量）上的真实回溯深度上限、时间粒度与频控限制分别是什么？
2. 面向个人研究与模拟的免费/低门槛美股行情、美股财报日历（Earnings Calendar）及宏观经济数据日历（Economic Calendar）公开 API（如 Alpaca、SEC EDGAR、FRED/ALFRED、Finnhub、FMP、Nasdaq 等）中，哪些提供可靠的事件预期时刻、实际公布时刻（是否精确到盘前/盘后或分钟秒级以支撑时点过滤）及合理使用条款？

## Answer

完整一手文档、限流参数、回溯上限对比表与条款来源见本地调研报告：[perp-and-calendar-data-sources.md](../../../docs/research/perp-and-calendar-data-sources.md)。核心事实结论如下：

1. **加密永续公开免密钥数据源能力与截断限制**：
   - **Bybit V5 Linear**：三家中唯一仅靠免密钥 REST + WebSocket 即可拿齐分钟 K 线、`10ms` BBO、`100ms` 实时 `Mark/Index/Funding/OI` 以及回溯至合约上线日的全历史 K 线、`Funding Rate` 和 `5min+` 历史 `Open Interest`（无 30 天或条数硬截断，公共 REST 限流 `600 req / 5s / IP`）。
   - **Binance USD-M**：公共 WS 无原生实时 OI 推送；REST 历史 OI（`/futures/data/openInterestHist`）硬上限仅保留**最近 30 天**，单次 K 线查询跨度 `<= 200 天`；超过 30 天的历史 `5m` OI 与多空比须通过官方免密钥归档站 `data.binance.vision/data/futures/um/daily/metrics` 批量补齐（注意 USD-M `bookTicker` 归档已于 `2024-03-30` 后停更）。
   - **Hyperliquid**：公共 WS 原生含区块级 BBO 与实时 `markPx / oraclePx / funding / openInterest`，但 REST `candleSnapshot` **仅保留最近 5,000 根 K 线**（`1m` 仅约 3.47 天、`5m` 约 17.36 天）且**无 REST 历史 OI 接口**（历史归档位于按月更新的 AWS Requester-Pays 桶 `s3://hyperliquid-archive/asset_ctxs`）。

2. **美股日内行情与 `Catalyst Calendar`（财报 + 宏观）数据源组合边界**：
   - **美股基础行情与新闻**：Alpaca Basic 免费层（个人非商业自用，`200 req/min`）提供自 2016 年起历史分钟线（查全市场 SIP 须延迟 `>= 15 分钟`）、免费实时 IEX 单场所 WS（上限 30 标的）及带 UTC 秒级 `created_at / updated_at` 的新闻流，但不含财报或宏观日历。
   - **美股财报日历双源拼装**：Finnhub 免费层（`60 calls/min`，个人非商业）提供未来排期及近 1 个月历史，精度为日期加 `bmo / amc / dmh` 时段标记（无分秒）；FMP 免费层已移除 `bmo/amc` 且限约 87 个示例标的；Nasdaq 官网日历接口条款禁止脚本抓取。历史真实时点防前视须结合免费免密钥的 **SEC EDGAR `submissions`（`8-K` Item `2.02` / `10-Q` / `10-K` 秒级 `acceptanceDateTime`）**，解析时须处理 `17:30 ET` 后 `filingDate` 顺延及美东时区归一化。
   - **宏观经济日历双源拼装**：Finnhub 宏观日历为付费专属；FRED/ALFRED API 免费提供发布日期与历史修订（`vintagedates`）但仅精确到天；盘中精确预期与公布钟点可由美国联邦机构免费公共领域机器可读日程表（**BLS `bls.ics`**、**BEA `schedule` JSON/ICS**、**美联储 FOMC 日历**，如 CPI/非农固定 `08:30 AM ET`、FOMC 固定 `2:00 PM ET`）直接补齐。
