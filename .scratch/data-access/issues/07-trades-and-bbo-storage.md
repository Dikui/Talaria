# 07 — 逐笔成交与买一卖一落盘，历史逐笔从公开归档补齐

**What to build:** 对实时订阅的标的，服务把买一卖一和逐笔成交按 `标的/日` 写入冷库；历史逐笔可以从 Bybit 公开归档按日补齐。SDK 能按时间范围和 `as_of` 读出。

**Blocked by:** 04 — Python 取数 SDK。

**Source of truth:** `docs/architecture.md` §4.1、§4.2；`docs/research/fill-simulation-and-tick-data.md`。

**Status:** ready-for-agent

- [ ] 行情传输层增加 `publicTrade.{symbol}` 订阅；L1 盘口沿用 `orderbook.1.{symbol}`。
- [ ] 逐笔与买一卖一分区写入，数值格式与 01 一致；不存交易 ID 以外的冗余字段（是否存交易 ID 在实现中按体积实测决定并记录）。
- [ ] 历史补数从 `public.bybit.com` 按日下载、校验，由服务写入（服务未运行时命令先取服务锁）；重复执行幂等。
- [ ] `sdk.trades(instrument, start, end, as_of)` 与 `sdk.quotes(...)` 支持 `as_of` 截断，与 K 线共用截断实现。
- [ ] 提供容量统计命令，按日输出各标的各数据集体积；落盘后记录首日实测写回 spec D3。1–2 周的容量结论以后再定，不在本单验收。
- [ ] 测试：半日 `as_of` 截断；重复补数幂等。

## Comments

### 实现记录（2026-11-22）

- Bybit 每个实时标的共享 `publicTrade` 和 `orderbook.1` 订阅。逐笔用撮合时刻 `T`，买一卖一优先用撮合时刻 `cts`。一分钟批量写入，退出时排空待写批次。逐笔以成交 ID 识别，买一卖一以事件毫秒识别。重发同值跳过，变化追加修订，实时版本的可获取时间取事件与接收时间的较晚值，并保持同一事件各修订的可获取时间单调。SDK 与 K 线共用先截断再选修订的实现，查询范围含两端，读取已落盘文件。
- 逐笔、买一卖一沿用清单、内容归档、质检、原子发布及退役保留流程。任务背景的月分区描述与架构 §4.2、ADR-0017 在此处有冲突，按后两者使用 `标的/类别/UTC 日`。Parquet 数值按列使用 `decimal128(18, s)`、物理 INT64。合并对已结束的 UTC 日分批执行。
- 逐笔保留 16 字节成交 UUID，用于同毫秒成交区分和归档补缺。生产布局写入同一合成 UTC 日各 64,000 条 BTCUSDT 数据后，逐笔 2,498,132 字节，去掉成交 ID 后 1,353,374 字节，差额约 17.9 字节/笔。买一卖一 1,571,294 字节。容量命令统计清单当前引用的 Parquet 行数与字节数，归档和待回收文件另占磁盘空间。
- `trade-backfill` 每次补一个已结束的 UTC 日。常驻服务在线时通过鉴权的 `POST /api/trades/backfill` 提交给其冷库写入线程，服务停机时命令先持同一数据目录锁。下载可注入，HTTPX 遵循交易所代理环境变量。导入前完整校验 gzip CRC、CSV 行、标的、UTC 日和数值精度。逐笔归档按 ID 只补缺，保留实时已写版本。归档首版按事件毫秒还原历史公开成交流的可获取时间。Bybit 归档与实时流是否逐条对应仍需用实盘样本核对。归档亚毫秒时间按 ADR-0017 截到毫秒，同毫秒跨批次或跨来源的撮合先后没有保证。
- 本机验收时，在独立的数据目录运行 `talaria up --solo --pack crypto_perp --data-dir "$dir"`，通过自选或图表持续订阅 BTCUSDT。UTC 日结束后等待下一次批写或正常停服务，再用 `talaria cold-size --data-dir "$dir" --day YYYY-MM-DD` 记录首个完整实时日的行数与字节数，写回 D3。另选一个已发布的归档日，在线及停机分别运行 `talaria trade-backfill BTC-USDT-PERP 2026-11-19 --data-dir "$dir"`，用 `cold-size` 和 `sdk.trades` 检查行数及半日截断。重复导入应新增 0 笔。沙箱中真实 WS、公开归档下载和首个完整实时日容量待验证。
- 局部回归 254 项通过。6 项 Bybit WebSocket 用例在沙箱绑定 `127.0.0.1` 时被拒绝，排除后复跑 254 项通过。内存 WebSocket、归档夹具、ASGI 常驻服务补数、SDK 半日截断、冷库合并、OpenAPI 快照和契约生成物均已验证。
