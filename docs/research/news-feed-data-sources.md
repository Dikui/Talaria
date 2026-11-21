# 快讯数据源（交易所公告、加密新闻、社交媒体、美股新闻）：一手官方资料核查

- **调研日期**：`2026-11-20`（`Asia/Shanghai`）。下文所有来源的访问日期均为 2026-11-20，不再逐条重复。
- **范围**：为 platform-redesign 需求中的“快讯”功能（加密新闻、交易所公告、宏观日历、社交媒体四类，见 `.scratch/platform-redesign/spec.md`，仅本地）核查数据源。关注点：结构化字段、发布时间戳精度、可回溯历史、实时推送方式、条款是否允许本地存储和自动化（策略、agent）使用，以及能否支撑防前视回放（`docs/architecture.md` 的 `ts_event / ts_published / ts_available / ts_ingested / ts_committed` 与 `PrecisionTier`）。
- **不重复的部分**：宏观日历（BLS / BEA / FOMC / FRED-ALFRED / Finnhub / FMP）、SEC EDGAR `submissions` 与 `acceptanceDateTime` 时点陷阱、Alpaca 行情免费层，已在 [perp-and-calendar-data-sources.md](./perp-and-calendar-data-sources.md) 第 1.1、3.1、3.2 节核查；新闻 `created_at / updated_at` 修订问题见 [trading-data-center-design.md](./trading-data-center-design.md)。本文只补空白。
- **方法**：只读访问官方 API 文档、官方定价页、官方条款和官方新闻稿。不注册账号，不调用需要密钥或收费的接口。唯一的接口调用是对 OKX、Bybit 公告接口（文档写明免密钥）的少量只读请求，用于核对历史深度。部分页面需 JS 渲染，用浏览器读取正文。
- **标注**：**事实** = 官方页面原文或实测结果；**厂商宣称** = 官方营销、新闻稿中的性能或覆盖承诺，未经验证；**推断** = 本文对 Talaria 的工程判断。价格和限额均为“截至访问日官方页面”，厂商可随时调整。本文不构成法律意见。

---

## 1. 核心结论

1. **三家交易所公告都能免费获取，但形态完全不同**（事实）。OKX 和 Bybit 提供免密钥 REST 分页列表，没有推送；Binance 只有需要 API Key 签名的 WebSocket 推送，官方文档没有历史 REST。OKX 返回 `pTime`（实际首次发布，毫秒）和 `businessPTime`（页面展示时间）两个字段，是三家中对防前视最友好的 [OKX-ann]；Bybit 的 `dateTimestamp` 由作者填写，另有 `publishTime` [BY-ann]。
2. **可合规交给策略和 agent 自动处理的免费加密新闻几乎没有**（事实 + 推断）。CryptoPanic 定价页已无免费档，最低为 \$50/周 [CP-plans]；CoinDesk Data API 已于 2026-05-21 取消免费层 [CD-free]，许可协议写明数据“not intended for trading purposes”[CD-lic]；Messari 新闻按 x402 每次 \$0.55 计费或走 Enterprise [MS-x402][MS-tier]；The Block 的 API 只对 The Block Pro 订阅机构开放 [TB-api]。CoinDesk、The Block、Cointelegraph 的 RSS 可公开访问，但三家条款都限制自动化收集或 AI 处理，The Block 和 Cointelegraph 写得最明确 [TB-tos][CT-tos][CDK-tos]。
3. **X API 现在完全按用量计费，没有订阅档位**（事实）。读取一条 Post 计 \$0.005，同一 UTC 日内重复读取同一资源只计一次，每月上限 300 万条 Post 读取，超出需 Enterprise [X-price]。按账号拉时间线（`GET /2/users/:id/tweets`，最多回溯该账号最近 3,200 条）和过滤流（1 条连接、1,000 条规则，P99 延迟约 4–5 秒）在按用量计费下都能用 [X-tl][X-fs]。条款要求离线存储的内容与 X 上的删除、修改保持同步，并禁止用于训练基础模型 [X-policy][X-dev]。
4. **Telegram 和 Truth Social 不宜作为自动化数据源**（事实）。Telegram 的 API 条款和 Bot 开发者条款都禁止把平台数据用于 AI/ML，其中 MTProto 条款还明确包括“deployment”（部署）[TG-api-tos][TG-bot-tos]。Truth Social 只有面向机构的授权数据产品 Truth API（2026-08-01 起提供），没有自助开发者 API [TS-pr]。
5. **美股新闻的免费组合是 Alpaca News + Finnhub + SEC EDGAR**（事实）。Alpaca News 有 REST 和 WebSocket，来源以 Benzinga 为主，覆盖股票和加密代码，提供秒级 `created_at` 与 `updated_at` [ALP-news-ref][ALP-news-ws]；Finnhub 免费层的公司新闻可回溯 1 年 [FH-cn]；Massive（原 Polygon.io）的新闻每小时更新，免费档只有 2 年历史，不适合做实时快讯 [MV-news][MV-price]。EDGAR 的 XBRL RSS 每 10 分钟更新 [SEC-rss]，实时分发服务 PDS 收费 [SEC-access]。

---

## 2. 交易所公告

### 2.1 Bybit V5：`GET /v5/announcements/index`

- **请求**（事实）：`locale` 必填，可选 `type`、`tag`、`page`（默认 1）、`limit`（默认 20，文档未写上限）。分页方式为 offset，返回 `total` [BY-ann]。
- **`locale` 枚举**（事实）：包括 `en-US`、`zh-TW`、`ja-JP` 等，**没有 `zh-CN`**。`type` 枚举：`new_crypto`、`latest_bybit_news`、`delistings`、`latest_activities`、`product_updates`、`maintenance_updates`、`new_fiat_listings`、`other`。`tag` 是多语言混合的自由标签（如 `Spot Listings`、`Derivatives`、`Delistings`）[BY-enum]。
- **字段**（事实）：`title`、`description`、`type{title,key}`、`tags[]`、`url`。`dateTimestamp` 为“Timestamp that author fills”（作者填写的时间）；`startDataTimestamp / endDataTimestamp` 只在 `latest_activities` 类型下有效，响应示例中拼写为 `startDateTimestamp`，与参数表不一致；`publishTime` 为“The published timestamp for the announcement”，但响应示例中没有出现 [BY-ann]。
- **鉴权与频控**（事实）：页面未写需要鉴权（官方 pybit 示例不传密钥）。HTTP 按 IP 默认限 600 次 / 5 秒，超限返回 `403, access too frequent`，需停 10 分钟 [BY-rl]。
- **实时推送**：文档中没有公告 WebSocket 频道（事实），只能轮询。
- **历史深度**：文档未说明。**实测**：本机出口 IP 请求 `api.bybit.com` 时，CloudFront 返回“configured to block access from your country”，未能取得数据。Bybit 接口可用性受所在地区限制，部署前需确认实际网络环境。

### 2.2 OKX：`GET /api/v5/support/announcements`

- **请求与鉴权**（事实）：鉴权可选。不带 `OK-ACCESS-KEY` 时按公共接口处理，“the response is restricted based on your request IP”（按请求 IP 限制返回内容）；带密钥时按账户居住国家限制。频控为每 2 秒 5 次（公共接口按 IP 计）。参数为 `annType`（取自 `GET /api/v5/support/announcement-types`，该接口每 2 秒 1 次）和 `page`，每页固定 20 条。`Accept-Language: en-US / zh-CN` 决定返回语言 [OKX-ann]。
- **字段**（事实）：`title`、`annType`、`url`；`pTime` 为“The actual time the announcement was first published”（实际首次发布时间，毫秒），并注明“Response may be delayed around 5 minutes”（返回可能延迟约 5 分钟）；`businessPTime` 为页面展示给用户参考的时间；返回 `totalPage`。按 `pTime` 和 `businessPTime` 倒序排列，公告更新后排序不变 [OKX-ann]。
- **实时推送**：文档的 WebSocket 部分只有系统状态频道，没有公告频道（事实）。
- **实测**（本机出口 IP，不带密钥，`en-US`）：`totalPage=7`，共约 131 条，最早一条为 2025-06-04，类型只有 `announcements-new-listings`、`announcements-delistings`、`trading-updates-us-aus`、`announcements-api` 4 种，URL 指向 `/en-us/help/`。说明公共模式下返回的是按 IP 所在地区裁剪的子集，**不是全站公告**。另外实测 `businessPTime` 与 `pTime` 不一致，相差可达约 1 小时，前后两个方向都有。
- **推断**：在 Talaria 中，`pTime` 可作为 `ts_published`（来源声称发布时间）。但它有“约 5 分钟延迟”的说明，所以只有实时轮询时本地记录的 `ts_ingested` 才是可靠的 `ts_available`；历史回填的公告应保守取 `pTime + 5 分钟` 作为 `ts_available`。`businessPTime` 不能用于时点过滤。

### 2.3 Binance：公告 WebSocket（需要 API Key）

- **连接**（事实）：`wss://api.binance.com/sapi/wss`，API Key 放在请求头 `X-MBX-APIKEY`，查询参数 `random`、`topic`、`recvWindow`（≤ 60000）、`timestamp` 和 HMAC-SHA256 `signature`。每条连接最长 24 小时；客户端每 30 秒发 PING，1 分钟没有 PING 即断开；每秒最多 5 条消息，超限断开，反复断线可能被封 IP [BN-ws-info]。
- **主题与字段**（事实）：`com_announcement_en`（只有英文）。`data` 是需要再解析一次的 JSON 字符串，包含 `catalogId`、`catalogName`（如 `Delisting`）、`publishDate`（毫秒）、`title`、`body`、`disclaimer` [BN-ann]。
- **历史**（事实）：公告文档只描述推送，没有历史 REST 接口。网站公告页使用的内部接口不在官方开发者文档中，本文不将其列为可用数据源。
- **推断**：需要一个 Binance 账户的 API Key（只读权限即可，但仍属于创建访问密钥，需要用户自己操作）。由于没有历史接口，Binance 公告只能从开始订阅之日起本地累积；订阅前的历史无法合规补齐。

---

## 3. 加密新闻

### 3.1 CryptoPanic API v2

- **定价**（事实，截至访问日官方页面）：Growth（按周）\$50/周，600 次请求/周；Growth \$199/月（年付 \$179/月），3,000 次请求/月，历史 1 个月，实时（“10 req/sec”和“No limit”两格只有两个值，对应三列，归属不清）；Enterprise 起价 \$899/月，历史 1 年，不限速，独享 Push API（webhook）和关键词搜索。功能亮点里写有“except Free plan”，但定价表中**已没有免费档** [CP-plans]。
- **接口**（事实）：`GET https://cryptopanic.com/api/<plan>/v2/posts/?auth_token=...`，`<plan>` 取 `developer / growth / enterprise`。可按 `currencies`、`regions`、`filter`（rising/hot/bullish/bearish/important…）、`kind` 过滤；`page` 最多 50 页；`size`、`search`、`last_pull`、`panic_period`、`with_content` 只对 Enterprise 开放 [CP-api]。
- **字段**（事实）：`published_at`（发布时间）与 `created_at`（进入 CryptoPanic 系统的时间）分开记录；`kind` 包括 news/media/blog/twitter/reddit；`instruments[]` 带代码；`votes`、`panic_score`。全文 `content` 只对 Enterprise 开放。另有 RSS 输出（`format=rss`，固定 20 条）[CP-api]。
- **条款**（事实）：网站使用条款最后更新于 2017-06-01，没有关于 API 数据存储或用途的具体条款 [CP-tos]。**推断**：存储和用于自动化研究的边界需要在购买时向厂商确认。

### 3.2 CoinDesk Data API（原 CryptoCompare）

- **接口**（事实）：`GET https://data-api.coindesk.com/news/v1/article/list`，参数 `lang`、`source_ids`、`categories`、`exclude_categories`、`limit`（1–100）、`to_ts`（取该时间及之前发布的文章，可向前翻页）；服务端缓存 10 秒。字段包括 `PUBLISHED_ON`（秒）加 `PUBLISHED_ON_NS`、`CREATED_ON`（系统内部创建时间）、`UPDATED_ON`、`STATUS`（ACTIVE/DELETED）、`SENTIMENT`（文档写明由 ChatGPT 生成）、`SOURCE_DATA`、`CATEGORY_DATA`、`BODY`（文档说明通常很有限）[CD-news]。
- **免费层**（事实）：官方博客 2026-04-17 宣布，“As of May 21, 2026”退役免费 API 层，没有订阅的账户届时停止返回数据 [CD-free]。官方定价页现在跳转到联系销售页面，没有公开价格。
- **许可**（事实，API Licence Agreement，2026-04-22）：仅限“your own internal use only”（自用，2.1）；不得把数据用于任何指数或金融产品（2.4.3，金融产品定义包括 token）；“the API Data is not intended for trading purposes”（10.1.1）；终止后必须删除已授权数据（11.5.3）[CD-lic]。
- **推断**：时间戳结构适合防前视（`CREATED_ON` 可近似作为供应商侧的 `ts_available`），但付费且条款与交易用途有冲突，不适合作为首批数据源。

### 3.3 Messari News API

- **接口**（事实）：`GET https://api.messari.io/news/v1/news/feed`，用请求头 `X-Messari-API-Key` 鉴权。支持 `publishedBefore/After`（RFC3339 或毫秒）、`sourceTypes`（News/Blog/Forum）、`assetIds`、`page/limit`。字段有 `publishTimeMillis`、`assets[]`、按资产给出的 `sentiment` 和 AI 摘要 [MS-feed]。
- **获取方式**（事实）：tier 页面说明新闻等附加服务需要联系销售谈 Enterprise [MS-tier]；也可以用 x402 按次付费，新闻 feed 每次 \$0.55，用 Base 或 Solana 上的 USDC 支付，并注明“pricing can change over time”（价格可能调整）[MS-x402]。
- **推断**：按 5 分钟轮询一次计算，每天 288 次，约 \$158/天，不适合用于实时快讯；可以考虑用于按需的历史研究查询。x402 需要用加密货币付款，是否使用由用户决定。

### 3.4 The Block

- **事实**：官方 2023-06-01 发布 News API（REST）和用于“market moving news”的 WebSocket，并写明“Licenses for all API tools are available to organizations with at least one subscription to The Block Pro”（API 授权只对至少有一份 The Block Pro 订阅的机构开放），没有公开价格和开发者文档 [TB-api]。
- **RSS 条款**（事实，第 7 节）：RSS 只能在“RSS feed reader”中使用，不得用于其他应用；第 4 节禁止“train artificial intelligence or machine learning using the Content”，也禁止用 AI 处理内容（包括摘要、改写）；第 6 节禁止系统性自动收集 [TB-tos]。

### 3.5 公开 RSS（CoinDesk、The Block、Cointelegraph、Decrypt）

- **实测**（事实）：四个 RSS 都可免密钥访问，`pubDate` 为 RFC-822 秒级时间（`+0000`）。单次返回 20–37 条，覆盖窗口约 1–3 天，**没有历史回溯**，也没有 `updated` 字段（Cointelegraph 带 `atom:updated` 除外）。
- **条款**（事实）：
  - CoinDesk（生效日 2026-10-30）：内容仅供“personal, informational, and non-commercial purposes”（个人、资讯、非商业用途）；使用“robot, spider, scraper, or other automated means”（机器人、爬虫等自动化手段）需书面许可 [CDK-tos]。
  - Cointelegraph：禁止“automated scraping, or creation of independent content pipelines”（自动抓取或建立独立内容管道）；禁止把内容用于 AI/ML/LLM 的“training, fine-tuning, validation, benchmarking or operation”（训练、微调、验证、评测或运行）[CT-tos]。
  - The Block：见 3.4。
- **推断**：RSS 最多只能在 Talaria 快讯页上以“阅读器”形式给人看（标题加原文链接）。不应进入冷库作为策略特征，也不应交给 agent 摘要或打分。

---

## 4. 社交媒体

### 4.1 X API v2（按用量计费）

- **计费**（事实，截至访问日官方页面）：“pay-per-usage pricing with no subscriptions”（只按用量计费，没有订阅），预先购买额度、按请求扣减。读取单价：Posts \$0.005/条，Users \$0.010/条，读取自己的数据 \$0.001/条。同一资源在同一个 UTC 日内去重计费，官方称这是“soft guarantee”（尽力保证）。每月上限 300 万条 Post 读取，更高用量需 Enterprise。没有免费档，只有首购赠送额度（\$20，另有首次自动充值匹配，上限 \$50）。页面注明“Prices are subject to change”（价格可能调整）[X-price]。文档首页同样说明 v2 按用量计费，v1.1 只有有限支持 [X-about]。
- **按账号拉时间线**（事实）：`GET /2/users/:id/tweets` 最多返回该账号“Up to 3,200 most recent Posts”（最近 3,200 条），支持 `start_time / end_time / since_id / until_id` [X-tl]。频控：应用级每 15 分钟 10,000 次 [X-rl]。
- **过滤流**（事实）：按用量计费下可以使用，限 1 条连接、1,000 条规则、每条规则最长 1,024 字符；Enterprise 为多连接、25,000 条以上规则，并提供 `embedding:` 语义运算符。官方称“approximately 4-5 seconds of P99 latency”（P99 延迟约 4–5 秒），更低延迟需要 Powerstream；连接每 20 秒发一次保活 [X-fs][X-rl]。
- **全量历史搜索**：`/2/tweets/search/all` 的频控为每秒 1 次、每 15 分钟 300 次 [X-rl]。哪些计费方式可以使用全量搜索，定价页和频控页都没有写明（未查到）。
- **时间戳**（事实）：`created_at` 为 ISO 8601，不是默认字段，需要用 `tweet.fields=created_at` 请求；`edit_history_tweet_ids` 默认返回 [X-dd]。
- **条款**（事实）：Developer Policy 规定，离线存储的 X 内容必须与 X 上的当前状态保持一致，删除或修改后要在合理时间内、或收到请求后 24 小时内同步处理；向第三方分发时原则上只能给 ID，每 30 天不超过 150 万个 Post ID [X-policy]。Developer Agreement（2026-04-27 更新）禁止“use the X API or X Content to fine-tune or train a foundation or frontier model”（用于微调或训练基础模型、前沿模型）[X-dev]。
- **推断**：
  - 成本估算：关注 50 个账号、平均每天每个账号 10 条，即 500 条/天 × \$0.005 ≈ \$2.5/天，约 \$75/月。关注 200 个高频账号约 \$300/月。实际费用取决于账号发帖频率。
  - 本地自用的 agent 分析、规则打分不在条款明文禁止之列，但必须实现删除同步。这与防前视回放的冷库“只追加、不可变”冲突：被删除的 Post 需要通过墓碑记录处理，而不是保留原文。这一点在设计时需要专门决定。

### 4.2 Telegram

- **Bot API**（事实）：Bot 能收到“All messages from channels where they are a member”（所在频道的全部消息，对应 `channel_post` 和 `edited_channel_post` 更新），看不到其他 Bot 发的消息；`getUpdates` 与 webhook 二选一，未取走的更新在服务器上最多保留 24 小时 [TG-faq][TG-botapi]。文档没有获取历史消息的方法。**推断**：只能读取你有权把 Bot 加入的频道（通常需要频道管理员同意），不能读取任意公开频道。
- **MTProto 用户账号**（事实）：需要自行申请 `api_id`。API 条款 1.5 禁止“using, accessing or aggregating data obtained from the Telegram platform to train, fine-tune or otherwise engage in the development, enhancement or deployment of artificial intelligence, machine learning models”（把平台数据用于训练、微调或开发、增强、部署 AI 和机器学习模型）[TG-api-tos]。
- **Bot 开发者条款**（事实，4.3）：明确禁止“any form of data collection aimed at creating large datasets, machine learning models and AI products, such as scraping public group or channel contents”（以建立大型数据集、机器学习模型和 AI 产品为目的的数据收集，例如抓取公开群组或频道内容）[TG-bot-tos]。
- **推断**：若 Telegram 内容要交给 agent 处理，不论用 Bot 还是 MTProto，都与条款冲突。最多只适合在快讯页上原样展示，并且不建议使用。

### 4.3 Truth Social

- **事实**：TMTG 于 2026-07-16 发布新闻稿，宣布 Truth API 为“business-to-business data feed”（企业对企业的数据馈送），预计 2026-08-01 起向机构客户提供，覆盖“highest-ranking Truth Social accounts”（排名最高的账号），并带 2022 年起的历史存档；稿中写明“Until now, no official, integrated API has existed”（此前没有官方 API）。没有公开价格，需要邮件联系授权 [TS-pr]。毫秒级送达属于**厂商宣称**。
- 没有面向个人开发者的官方 API 或文档（未找到一手资料）。第三方“Truth Social API”均为抓取服务，不在本文考虑范围。

---

## 5. 美股新闻

### 5.1 Alpaca News API

- **REST**（事实）：`GET https://data.alpaca.markets/v1beta1/news`，参数 `symbols`（股票和加密代码都可以，如 `BTCUSD`）、`start/end`、`limit`（1–50）、`sort`（按**更新时间**排序）、`include_content`、`exclude_contentless`、`page_token`。没有实时权限时，`end` 默认是当前时间减 15 分钟；订阅不覆盖所请求数据时返回 403 [ALP-news-ref]。
- **WebSocket**（事实）：`wss://stream.data.alpaca.markets/v1beta1/news`，订阅 `{"action":"subscribe","news":["*"]}`。消息字段为 `id / headline / summary / author / created_at / updated_at / url / content / symbols / source`；示例中的 `source` 有 `benzinga` 和 `fiscal.ai` 两种 [ALP-news-ws]。
- **来源与历史**（官方博客，2022-02-07，可能已过时）：来自 Benzinga，可回溯到 2015 年，每天 600–900 条实时标题；当时为限时免费 beta，免费账户 200 次/分钟 [ALP-blog]。当前行情计划表没有单独列出新闻的限额 [ALP-plans]。**未查到**：Basic 免费账户的新闻 WebSocket 是否实时，以及 REST 新闻是否也有 15 分钟延迟的限制。
- 条款上的非专业用途、不得再分发等限制已在 [perp-and-calendar-data-sources.md](./perp-and-calendar-data-sources.md) 3.1 节核查。

### 5.2 Finnhub

- **事实**：`/company-news` 仅限北美公司，“Free Tier: 1 year of historical news and new updates”（免费层可取 1 年历史和新增内容），日期参数为 `YYYY-MM-DD`；`/news?category=general|forex|crypto|merger` 支持 `minId` 增量拉取；字段 `datetime` 为 UNIX 秒，另有 `headline / source / summary / url / related`。新闻 WebSocket、新闻情绪、新闻稿都标为 Premium。所有套餐另有每秒 30 次的总上限 [FH-cn]。免费层每分钟 60 次、限个人用途，见既有调研 [FH-price]。
- **推断**：`datetime` 只有一个发布时间，没有入库时间和修订时间，回填数据只能标为低精度等级。

### 5.3 Massive（原 Polygon.io）

- **事实**：polygon.io 已 301 跳转到 massive.com。`GET /v2/reference/news` 所有股票套餐都可用，字段包括 `published_utc`、`insights[].sentiment`、`publisher`、`tickers`；`limit` 最大 1,000；数据最早到 2016-06-22，Basic 免费档只能取 2 年；“Updated hourly on all plans”（所有套餐均每小时更新）[MV-news]。Stocks Basic \$0/月，每分钟 5 次，仅限个人用途；Benzinga 实时新闻作为合作数据集单独销售，\$99/月/数据集 [MV-price]。
- **推断**：每小时更新的批量新闻不能作为快讯源。`ts_available` 至少要按 `published_utc` 加 1 小时保守估计。

### 5.4 SEC EDGAR 实时性（补充）

- 基础能力已有调研覆盖。补充事实：XBRL RSS“updated every ten minutes Monday through Friday, 6am – 10pm EST”（工作日 6:00–22:00 每 10 分钟更新）[SEC-rss]；Latest Filings 可按公司、CIK、表单类型订阅为 Atom [SEC-rssg]；当日索引在约 22:00 ET 开始的夜间批处理中生成；实时分发服务 PDS 为“Fee-based”（收费）[SEC-access]。
- **推断**：免费方式下，EDGAR 快讯的延迟以分钟计（轮询 Latest Filings Atom，遵守 10 次/秒限制）。`ts_available` 应取本地拉取到的时间，而不是 `acceptanceDateTime`。

---

## 6. 宏观日历（只补空白）

既有调研已覆盖 BLS、BEA、FOMC 日程和 FRED/ALFRED。补充两个官方免费源：

- **美联储 RSS**（事实）：`press_all.xml`、`press_monetary.xml`、`speeches.xml`、`testimony.xml` [FED-rss]。实测 `pubDate` 为秒级 GMT（例如 FOMC 会议纪要 `Wed, 18 Nov 2026 19:00:00 GMT`）。适合用作“已发布”事件的 `ts_published`。
- **美国国债拍卖**（事实）：TreasuryDirect `GET /TA_WS/securities/upcoming?format=json` 免密钥，返回 `announcementDate`、`competitiveAuctionCloseDate`、`competitiveAuctionCloseTime` 等（实测）。官方已发布字段改名通知 [TD-chg]；Fiscal Data 有“Upcoming Auctions”数据集 [FD-auc]。
- **未查到**：非美央行（ECB 等）日历，以及加密专属事件（代币解锁、主网升级）的免费官方日历。Messari 的 Token Unlocks 属于 Enterprise 附加服务 [MS-tier]。

---

## 7. 汇总表

“免费可用性”按截至 2026-11-20 的官方页面填写。

| 来源 | 类别 | 免费可用性 | 实时方式 | 历史深度 | 时间戳字段 | 条款限制 |
|---|---|---|---|---|---|---|
| Bybit `/v5/announcements/index` | 交易所公告 | 免费，文档未要求密钥；按 IP 600 次/5 秒；**本机 IP 被地区封锁** | 无推送，只能轮询 | 文档未说明（未能实测） | `dateTimestamp`（作者填写）、`publishTime`（ms） | 受地区访问限制 |
| OKX `/api/v5/support/announcements` | 交易所公告 | 免费，鉴权可选；5 次/2 秒 | 无推送，只能轮询；`pTime` 可能延迟约 5 分钟返回 | 实测本 IP 约 131 条，最早 2025-06；按 IP 地区裁剪 | `pTime`（实际首发，ms）、`businessPTime`（展示用） | 内容按 IP 或居住国裁剪 |
| Binance `com_announcement_en` | 交易所公告 | 需要账户 API Key 签名，无费用 | WebSocket 推送，单条连接最长 24 小时 | 文档无历史接口 | `publishDate`（ms） | 每秒 5 条消息，反复断线可能封 IP |
| CryptoPanic API v2 | 加密新闻聚合 | **无免费档**；\$50/周或 \$199/月；Enterprise 起价 \$899/月 | 轮询；Enterprise 有 webhook | 1 个月（Growth）/ 1 年（Enterprise） | `published_at`、`created_at` | 条款未涉及 API 用途，需向厂商确认 |
| CoinDesk Data API 新闻 | 加密新闻 | **免费层已于 2026-05-21 取消**；价格需询价 | 轮询（缓存 10 秒） | 可按 `to_ts` 向前翻页，深度未写明 | `PUBLISHED_ON(+_NS)`、`CREATED_ON`、`UPDATED_ON` | 仅限自用；“not intended for trading purposes”；终止后须删除 |
| Messari News | 加密新闻 | Enterprise，或 x402 每次 \$0.55 | 轮询 | 支持日期范围，深度未写明 | `publishTimeMillis` | 未查到 |
| The Block News API | 加密新闻 | 仅限 The Block Pro 订阅机构 | 有 WebSocket（官方宣称） | 未公开 | 未公开 | 需授权；RSS 只能用于阅读器，禁止 AI 处理 |
| CoinDesk / The Block / Cointelegraph / Decrypt RSS | 加密新闻 | 可公开访问 | RSS 轮询 | 约 1–3 天 | `pubDate`（秒） | 非商业；限制自动化；The Block 和 Cointelegraph 明确禁止 AI 处理或运行；Decrypt 未核查 |
| X API v2 | 社交媒体 | **无免费档**；Post 读取 \$0.005/条，每月上限 300 万条 | 过滤流（P99 约 4–5 秒）、时间线轮询 | 时间线为该账号最近 3,200 条；全量搜索的可用范围未查到 | `created_at`（ISO 8601，需显式请求）、编辑历史 | 必须同步删除和修改；禁止训练基础模型；对外分发只能给 ID |
| Telegram Bot API / MTProto | 社交媒体 | 免费 | Bot 有 webhook 或长轮询；MTProto 有更新推送 | Bot 无历史接口；更新最多保留 24 小时 | Message `date`（秒）* | **禁止用于 AI/ML（含部署）**；禁止抓取频道建数据集 |
| Truth Social Truth API | 社交媒体 | 仅限机构授权，无公开价格 | 推送（厂商宣称毫秒级） | 2022 年起（厂商宣称） | 未公开 | 需授权 |
| Alpaca News | 美股 + 加密新闻 | 免费 Basic 账户可用（需注册取 Key）；实时程度未查到 | WebSocket + REST | 2015 年起（2022 年官方博客） | `created_at`、`updated_at`（RFC-3339） | 非专业用途，不得再分发（既有调研） |
| Finnhub 新闻 | 美股新闻 | 免费层：公司新闻 1 年、市场新闻 | REST 轮询（新闻 WS 为 Premium） | 1 年（免费层） | `datetime`（UNIX 秒） | 免费层限个人用途（既有调研） |
| Massive（Polygon）新闻 | 美股新闻 | Basic \$0，每分钟 5 次 | **每小时批量更新** | 2 年（Basic），最早 2016-06-22 | `published_utc` | 个人用途 |
| SEC EDGAR Latest Filings / XBRL RSS | 美股公告 | 免费，免密钥，10 次/秒 | Atom/RSS 轮询，XBRL 每 10 分钟；实时 PDS 收费 | 全量（既有调研） | `acceptanceDateTime` 等（既有调研） | 公共领域，须声明 User-Agent |
| 美联储 RSS、TreasuryDirect 拍卖 | 宏观日历补充 | 免费 | RSS 或 JSON 轮询 | RSS 只有近期；拍卖可查询历史 | `pubDate`（秒）；拍卖日期 + 截止时刻 | 美国政府公开数据 |

\* Telegram `Message.date` 字段来自 Bot API 对象定义，本文未逐字核对。

---

## 8. 对 Talaria 的启示（推断，供决策参考，不替用户做最终决定）

### 8.1 时间戳映射建议

| 来源 | `ts_published` | `ts_available`（实时） | `ts_available`（历史回填） | 建议 `PrecisionTier` |
|---|---|---|---|---|
| OKX 公告 | `pTime` | 本地拉取时间 | `pTime + 5 min` | 实时为 `SECOND_VERIFIED`，回填降一级 |
| Bybit 公告 | `publishTime`（字段存在时） | 本地拉取时间 | 无可靠依据，只能保守估计 | 回填为 `DATE_CONSERVATIVE` |
| Binance 公告 | `publishDate` | 本地收到推送的时间 | 不可回填 | `SECOND_VERIFIED` |
| Alpaca News | `created_at` | 本地收到时间；更新以 `updated_at` 追加新版本 | `created_at` 加保守延迟 | 回填降一级 |
| X | `created_at` | 本地收到时间 | `created_at`（可能已被编辑或删除） | 回填降一级 |
| Massive 新闻 | `published_utc` | 不适用 | `published_utc + 1 h` | 低 |

共同原则：只有 Talaria 自己实时采集并写入 `ts_ingested` 的记录，才能在 `OBSERVED_REPLAY` 中精确回放。所以快讯采集**越早开始常驻运行越好**，历史回填只能用作低精度数据。

### 8.2 首批数据源组合与成本

| 方案 | 内容 | 月成本（截至访问日） | 覆盖缺口 |
|---|---|---|---|
| **A：零现金成本** | OKX、Bybit 公告轮询（需确认网络地区）；Binance 公告 WS（用户自建只读 Key）；Alpaca News WS + REST（美股和加密，Benzinga）；Finnhub 市场和公司新闻；SEC EDGAR Latest Filings；宏观沿用既有方案，加美联储 RSS 和国债拍卖；加密媒体 RSS 只在快讯页给人看（标题加链接，不进冷库、不交给 agent） | \$0 | 没有加密原生媒体的合规机读新闻；没有社交媒体 |
| **B：A + X** | 在 A 的基础上，用 X 过滤流或时间线跟踪一份精选账号名单（交易所官方号、项目方、宏观和政策账号） | 约 \$75–300（取决于账号数和发帖量；需预充额度） | 需要实现删除同步；关注名单需要维护 |
| **C：B + 加密新闻聚合** | 在 B 的基础上加 CryptoPanic Growth（实时，1 个月历史） | 再加 \$199（或 \$50/周试用） | 每月 3,000 次请求，轮询间隔需大于等于约 15 分钟，或改用更高档；条款需向厂商确认 |

不推荐作为首批：CoinDesk Data（无公开价格，且许可写明不用于交易）、Messari x402（按次付费太贵）、The Block（需机构订阅）、Telegram（条款禁止 AI 用途）、Truth API（机构价格）、Massive 新闻（每小时更新）。

### 8.3 需要用户决定的事项

1. 是否愿意为 Binance 公告创建一个只读 API Key（需要用户自己操作）。
2. 是否接受 X 按用量计费，以及月度预算上限。X 平台支持设置花费上限 [X-price]。
3. 对已删除的 X 内容在冷库中如何处理（墓碑记录还是物理删除），这会影响 ADR-0011 的只追加约定。
4. 加密媒体 RSS 是否只用于展示。如果希望 agent 也能读加密新闻，需要预算付费源（方案 C），或者用 Alpaca News 的加密覆盖替代。

---

## 9. 未查到一手资料的问题

- Bybit 公告接口的历史深度和 `limit` 上限（文档未写；本机 IP 被 CloudFront 地区封锁，无法实测）。
- Binance 公告是否有官方历史接口（官方文档未提供）。
- Alpaca Basic 免费账户的新闻 WebSocket 是实时还是有延迟，以及新闻是否有单独的限额（当前计划表未列出新闻）。
- X 全量历史搜索 `/2/tweets/search/all` 在按用量计费下是否可用、如何计费（定价页和频控页均未说明）。
- CryptoPanic API 数据的存储和自动化使用条款（网站条款为 2017 年版本，未涉及 API）。
- Messari 新闻的历史深度和 Enterprise 价格；The Block API 的价格和字段。
- Decrypt RSS 的使用条款（未核查）。
- 非美央行日历和加密专属事件日历的免费官方源。

---

## 10. 一手来源索引（均访问于 2026-11-20）

### 交易所公告
- **[BY-ann]** Bybit V5 Get Announcement：https://bybit-exchange.github.io/docs/v5/announcement
- **[BY-enum]** Bybit V5 Enums（`locale` / `announcementType` / `announcementTag`）：https://bybit-exchange.github.io/docs/v5/enum
- **[BY-rl]** Bybit V5 Rate Limit Rules：https://bybit-exchange.github.io/docs/v5/rate-limit
- **[OKX-ann]** OKX API v5 — Announcement（Get announcements / Get announcement types）：https://www.okx.com/docs-v5/en/#announcement-get-announcements
- **[BN-ws-info]** Binance Announcements — WebSocket API Basic Information：https://developers.binance.com/en/docs/products/announcements/general-info
- **[BN-ann]** Binance Announcements — Announcement（`com_announcement_en`）：https://developers.binance.com/en/docs/products/announcements/announcement

### 加密新闻
- **[CP-api]** CryptoPanic API Reference：https://cryptopanic.com/developers/api/
- **[CP-plans]** CryptoPanic API Plans & Pricing：https://cryptopanic.com/developers/api/plans
- **[CP-tos]** CryptoPanic Terms of Use：https://cryptopanic.com/terms/
- **[CD-news]** CoinDesk Data API — News Latest Articles：https://developers.coindesk.com/documentation/data-api/news_v1_article_list
- **[CD-free]** CoinDesk Data — Changes to CoinDesk Data & Indices API Free Tier Access（2026-04-17）：https://data.coindesk.com/blogs/changes-to-coindesk-data-indices-api-free-tier-access
- **[CD-lic]** CoinDesk Data API Licence Agreement（2026-04-22）：https://data.coindesk.com/api-licence-agreement
- **[MS-feed]** Messari Get News Feed：https://docs.messari.io/api-reference/endpoints/news/get-v1-news-feed
- **[MS-tier]** Messari API Tiers & Limits：https://docs.messari.io/api-reference/permissions
- **[MS-x402]** Messari Pay-Per-Request with x402：https://docs.messari.io/api-reference/x402-payments
- **[TB-api]** The Block Introduces News API（2023-06-01）：https://www.theblock.co/post/232778/the-block-unveils-real-time-news-api-for-seamless-integration-of-its-sector-specific-coverage-of-crypto-and-digital-assets
- **[TB-tos]** The Block Terms of Service：https://www.theblock.co/terms-of-service
- **[CDK-tos]** CoinDesk Terms of Use（生效日 2026-10-30）：https://www.coindesk.com/terms
- **[CT-tos]** Cointelegraph Terms and Privacy：https://cointelegraph.com/terms-and-privacy
- RSS 实测地址：https://www.coindesk.com/arc/outboundfeeds/rss/ 、https://www.theblock.co/rss.xml 、https://cointelegraph.com/rss 、https://decrypt.co/feed

### 社交媒体
- **[X-price]** X API Pricing：https://docs.x.com/x-api/getting-started/pricing
- **[X-about]** About the X API：https://docs.x.com/x-api/getting-started/about-x-api
- **[X-rl]** X API Rate Limits：https://docs.x.com/x-api/fundamentals/rate-limits
- **[X-tl]** X API Timelines Introduction：https://docs.x.com/x-api/posts/timelines/introduction
- **[X-fs]** X API Filtered Stream Introduction：https://docs.x.com/x-api/posts/filtered-stream/introduction
- **[X-dd]** X API Data Dictionary：https://docs.x.com/x-api/fundamentals/data-dictionary
- **[X-policy]** X Developer Policy：https://docs.x.com/developer-terms/policy
- **[X-dev]** X Developer Agreement（2026-04-27 更新）：https://docs.x.com/developer-terms/agreement
- **[TG-faq]** Telegram Bots FAQ：https://core.telegram.org/bots/faq
- **[TG-botapi]** Telegram Bot API（Getting updates、`channel_post`）：https://core.telegram.org/bots/api
- **[TG-api-tos]** Telegram API Terms of Service：https://core.telegram.org/api/terms
- **[TG-bot-tos]** Telegram Bot Platform Developer Terms：https://telegram.org/tos/bot-developers
- **[TS-pr]** TMTG Launches Truth API（GlobeNewswire，2026-07-16）：https://www.globenewswire.com/news-release/2026/7/16/3328489/0/en/Trump-Media-and-Technology-Group-Launches-Truth-API-a-New-Licensed-Data-Service-for-Financial-Services-Partners-That-Provides-the-Fastest-Access-to-Truth-Social-s-Most-Influential-.html

### 美股新闻与公告
- **[ALP-news-ref]** Alpaca News API Reference：https://docs.alpaca.markets/us/reference/news-3
- **[ALP-news-ws]** Alpaca Real-time News：https://docs.alpaca.markets/docs/streaming-real-time-news
- **[ALP-blog]** Alpaca — Introducing News API（2022-02-07）：https://alpaca.markets/blog/introducing-news-api-for-real-time-fiancial-news/
- **[ALP-plans]** Alpaca About Market Data API：https://docs.alpaca.markets/docs/about-market-data-api
- **[FH-cn]** Finnhub API Documentation（Company News / Market News / 侧栏 Premium 标记）：https://finnhub.io/docs/api/company-news
- **[FH-price]** Finnhub Pricing：https://finnhub.io/pricing （引自既有调研）
- **[MV-news]** Massive（原 Polygon.io）Stocks News：https://massive.com/docs/rest/stocks/news
- **[MV-price]** Massive Pricing：https://massive.com/pricing
- **[SEC-rss]** SEC Interactive Data RSS Feeds：https://www.sec.gov/structureddata/rss-feeds
- **[SEC-rssg]** SEC RSS Feeds：https://www.sec.gov/about/rss-feeds
- **[SEC-access]** SEC Accessing EDGAR Data：https://www.sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data

### 宏观补充
- **[FED-rss]** Federal Reserve RSS Feeds：https://www.federalreserve.gov/feeds/feeds.htm
- **[TD-chg]** TreasuryDirect Auction File & API Specification Changes：https://www.treasurydirect.gov/files/auction/auction-file-&-api-specification-changes.pdf
- **[FD-auc]** Fiscal Data — Treasury Securities Upcoming Auctions：https://fiscaldata.treasury.gov/datasets/upcoming-auctions/
