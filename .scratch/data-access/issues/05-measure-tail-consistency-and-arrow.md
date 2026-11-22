# 05 — 实测：补尾与直读的并发一致性、Arrow IPC 传输耗时

**What to build:** 两个原型实测，给 04 的实现定结论：① 服务持续写入和合并时，SDK 直读加补尾会不会漏柱、重柱；② 千万行经本机 HTTP 用 Arrow IPC 传输的耗时，判断范围接口是否需要 Arrow 格式。

**Blocked by:** 04 — Python 取数 SDK。

**Source of truth:** ADR-0016、ADR-0018。

**Status:** resolved

- [ ] 一致性：高频写入 + 合并期间反复读取，统计漏柱、重柱、读到旧文件被删等错误次数；另让一个读者读出清单后暂停，跨过合并和清理再继续，验证保留期与重试是否足够。给出复现脚本（原型代码不提交）。
- [ ] Arrow IPC：1M、10M 行在 JSON 与 Arrow IPC 下的传输与解析耗时。
- [ ] 结论写回 `.scratch/platform-redesign/spec.md` 的 D3“待实测”处；发现缺陷时新开任务单。

## Comments

### 实现记录（2026-11-22）

#### 补尾与直读

通过进程内 HTTP 运行真实范围路由和 SDK。合成 BTC 柱经 `FeedSupervisor`、热缓冲与冷库写入器持续发布，真实合并任务的调度间隔缩短至 40 ms。两名读者分别查询最新值和给定 `as_of` 的值，每次以发起查询前已确认的柱为期望范围。从 32 根已归档柱开始，分别注入 360 和 480 根确认柱。单根实际用时包含投递开销。

| 投递目标间隔 | 写入确认柱 | 合并换代 | 查询数 | 与发布重叠 | 与合并重叠 | 仅冷库结果 | 缺柱查询 | 累计缺柱 | 重柱 | 文件缺失 / 查询异常 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 8 ms | 360 | 50 | 135 | 133 | 97 | 75 | 1 | 1 | 0 | 0 / 0 |
| 1 ms | 480 | 5 | 18 | 18 | 10 | 17 | 12 | 1,222 | 0 | 0 / 0 |

累计缺柱按每次查询加总，同一柱可以出现在多次查询的缺失集合里。两轮均未发现返回值错误、排序错误或范围外的柱。所有少柱查询均标记 `tail_completed=False`。写入排空后分别返回 392 和 512 根。

受控复现中，延后 BTC 柱 B 的健康写入，两次补尾响应都包含 B。每次响应之后发布 ETH 柱，推进全局清单。SDK 返回一根而期望两根，并标记未补尾。两条指定 ADR 及架构 §4.3 的离线语义与任务目标一致。04 在服务在线时连续换代后的降级会遗漏已确认热端柱，已记录为 [data-access/09](09-preserve-sdk-tail-during-manifest-churn.md)。

慢读者在选出清单文件后、第一次打开文件前暂停。用 `ColdStore.compact(now_ms=...)` 推进回收时钟。在满 3 代但未满 24 小时、满 24 小时但未满 3 代两种场景中，旧文件继续可读。两个条件均满足后，旧文件被回收，读者遇到一次 `FileNotFoundError`，重新按清单打开合并文件并读回预期的两根柱。三个场景均无查询报错。

#### HTTP 传输与解析

macOS arm64，Python 3.15.2，PyArrow 26.0.1，NumPy 2.5.5。相同合成 K 线字段，JSON 使用生产 `RangeBar` 序列化的十进制字符串，Arrow 使用 decimal128 和字典编码的精度字段。预编码文件以 10,000 行分批组成一个无压缩 HTTP/1.1 响应。传输计时从请求发起到读完响应体。解析单独计时，从预编码文件读出 JSON 字典或 Arrow RecordBatch。每项执行 3 次，下表为中位数。计时范围外包括编码、Parquet 读取、服务查询及解析后的业务对象构建。

| 行数 | 格式 | 响应体字节 | 传输（秒） | 解析（秒） |
| ---: | --- | ---: | ---: | ---: |
| 10,000 | JSON | 2,288,504 | 0.0024 | 0.0086 |
| 10,000 | Arrow IPC | 1,212,800 | 0.0013 | 0.0002 |
| 1,000,000 | JSON | 228,842,952 | 0.2167 | 0.9165 |
| 1,000,000 | Arrow IPC | 121,188,920 | 0.1186 | 0.0076 |
| 10,000,000 | JSON | 2,288,428,814 | 2.1337 | 8.9181 |
| 10,000,000 | Arrow IPC | 1,211,880,920 | 1.2194 | 0.0761 |

两种格式的逐字段往返已用小样本验证。沙箱拒绝绑定 TCP 回环和命名 Unix 套接字，以上 HTTP 字节经匿名 `socketpair` 传输。TCP 回环耗时、常驻服务端到端吞吐、真实交易所与生产负载仍待验证。现有 10,000 根范围上限保留 JSON，大批量本机历史继续按清单直读。结论已写回 [D3](../../platform-redesign/spec.md#d3-统一取数接口与历史数据存储2026-11-20)。

#### 复现与验证

原型在本工作区的 `.scratch/prototype/data-access-05/`，仅本地。以下命令均从工作区根执行，公共前缀为 `PYTHONPATH=$PWD/core/src /Users/dikui/Project/Talaria/.venv/bin/python`。

| 前缀后的参数 | 实际结果 |
| --- | --- |
| `-m pytest tests/sdk tests/workbench_api/test_bars_api.py tests/perception/test_cold_store.py tests/perception/test_cold_publisher.py -q -p no:cacheprovider` | 81 通过，0 失败 |
| `-m pytest .scratch/prototype/data-access-05/test_measurement.py -q -p no:cacheprovider` | 实现前 1 个收集错误（原型模块待实现），实现后 2 通过，0 失败 |
| `.scratch/prototype/data-access-05/tail_consistency.py --scenario all` | 初跑完成压力场景后，原型的临时目录路径断言失败。统一解析路径后，完成压力、慢读者和受控复现 |
| `.scratch/prototype/data-access-05/tail_consistency.py --scenario stress --live-rows 480 --cadence 0.001` | 完成压力运行，18 次查询，12 次缺柱 |
| `.scratch/prototype/data-access-05/tail_consistency.py --scenario forced --assert-complete` | 1 个预期失败，复现两次换代后缺少 B |
| `.scratch/prototype/data-access-05/arrow_http.py --rows 10000 1000000 10000000` | 完成 3 种行数、2 种格式、各 3 次传输与解析 |
| `.scratch/prototype/data-access-05/arrow_http.py --transport tcp --rows 10000 --repeats 1` | TCP 回环绑定被沙箱拒绝，退出码 2。沙箱外可用 `--transport tcp --rows 10000 1000000 10000000` 复测 |

逐路径执行 `git add --` 时，沙箱拒绝在主仓库的 worktree Git 元数据目录创建 `index.lock`，返回 128。改动保留在 `ticket/data-access-05` 工作区，HEAD 为 `5e48db6`。
