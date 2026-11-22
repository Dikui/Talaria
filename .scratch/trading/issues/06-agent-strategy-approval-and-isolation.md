# 06 — agent 写的 Python 策略：批准与隔离运行

**What to build:** agent 写的 Python 策略可以自由回测；要进模拟盘，交易员必须批准与代码内容哈希绑定的版本，代码一改批准即失效。进模拟盘的 agent 代码在资源受限、无网络、读不到数据目录的子进程中运行，只能经宿主传入的已截断数据取数。

**Blocked by:** 01 — 账户进程宿主。

**Source of truth:** ADR-0015（agent 生成的代码）、ADR-0019（Python 的运行位置）；`docs/architecture.md` §6.1。

**Status:** ready-for-agent

- [ ] 策略代码登记：来源（人工 / agent）、内容哈希、批准记录（批准人、时刻、哈希）。
- [ ] 宿主在 `--mode paper` 启动时检查：agent 来源且未批准或哈希不符即拒绝启动并说明原因。
- [ ] agent 代码在子进程中运行：CPU 时间、内存、单次回调耗时有上限；无网络；读数据目录失败；与宿主之间只传已截断的数据与策略输出。
- [ ] 测试：未批准、批准后改代码、超时或超内存三种情形；子进程内尝试打开冷库 Parquet 失败。

## Comments

### 实现记录

- `talaria account strategy-register <file.py> --source human|agent` 按解析后的文件路径在数据目录的 `strategy-code.sqlite` 登记来源和 SHA-256。`talaria account strategy-approve <file.py> --sha256 <已审阅的哈希> --approved-by <交易员>` 记录批准人、UTC 时刻与哈希。重新登记同一版本保留批准，登记修改后的版本清空批准。登记库和命令调用方使用本机交易员的权限。
- `account run --mode paper` 经账户宿主检查文件策略的登记与批准。宿主执行检查过的同一份字节，agent 策略的模块加载、构造和回调都在子进程内完成。人工登记的文件策略由账户进程加载。已注册的宿主插件继续使用现有插槽接口，回测不要求批准。模拟盘的实时数据和账本接线由 trading/04 提供。
- 子进程在执行策略前启用 macOS Seatbelt，默认拒绝文件、网络和进程访问，只允许读取 Python 标准库，策略数据目录和 site-packages 始终禁止读取。策略依赖已装入的 Talaria 组件与标准库。进程通过有界 JSON 管道调用宿主的 `ctx`，宿主只返回当前 `as_of` 允许读取的值。输出经宿主重新校验并进入原定仓与风控流程。
- 启动上限 10 秒，单次回调上限 2 秒，单次回调最多 64 次取数。子进程在启动基线之上有 256 MiB 虚拟地址空间分配额度，CPU 预算为每进程 60 秒。宿主独立监测 CPU 用量并回收超限子进程。隔离初始化失败即拒绝启动。未发现与指定 ADR 或架构 §6.1 的冲突。本单无删除类验收项。
- 局部回归 `pytest tests/account tests/components tests/foundation/test_import_boundaries.py -q -p no:cacheprovider --tb=short` 最终 142 通过、1 失败，另有一条既有 Starlette 弃用警告。`contract-check` 零漂移。
- 唯一失败项为 `tests/account/test_strategy_isolation.py::test_production_worker_cannot_read_cold_parquet_or_open_network`。当前沙箱拒绝启用嵌套 Seatbelt（`Operation not permitted`），已验证启动拒绝，真实冷库和网络拒绝行为需在沙箱外 macOS 验证。测试注入层验证了真实 Python 子进程的资源限制、取数与输出边界。未连接 DSH，未运行全量测试。
- 已检查 `git diff main...HEAD` 和工作区改动。沙箱拒绝创建 worktree 的 `index.lock`，变更保留在 `ticket/trading-06` 工作区。预定提交信息为 `feat: 增加模拟盘策略批准与隔离`。

### 审查修复

- 回测与模拟盘共用文件策略准入。策略进程在启用 Seatbelt 后加载模块、校验插件及组件参数 Schema、构造实例并执行回调。宿主只接受就绪信号和有界的策略输出。未登记或人工登记后内容已变的策略在回测中按 agent 隔离，模拟盘要求登记哈希匹配，agent 还须有同哈希的人工批准。
- 本机交易员通过 `account strategy-register --source human|agent` 确认来源。运行入口仅读取登记库。回测方案显式引用的其他宿主文件插件也须将当前版本登记为 `human`，避免把同一份策略文件换到数据、定仓、风控或成交插槽后在宿主加载。宿主按名称预装的插件沿用原注册方式。
- 快照包含入口名、策略目录内递归收集的普通 `.py` 文件的相对路径和原始字节。按路径排序、固定归档时间，对完整归档做 SHA-256。批准与按哈希存储的归档在同一 SQLite 事务写入。启动准入时核对工作目录，之后从核验过的归档复制出只读临时目录。入口或导入模块的修改都需要重新审阅，旧版单文件哈希需要重新登记、批准。
- 登记和准入在写事务及子账户锁之前完成文件校验。读取通过目录描述符和 `os.open(O_NONBLOCK | O_NOFOLLOW)` 打开文件，用 `fstat` 检查普通文件和大小，再做有界读取。单文件上限 1 MiB，每个快照最多 256 个文件、8 MiB 源码。Seatbelt 仅读取所需标准库、当前环境的依赖、Talaria 包目录及临时快照，明确禁止读写数据目录，忽略环境中的 `PYTHONPATH`。
- 宿主对 IPC 取数按实际底层时间范围估算，计入跨周期预热及衍生品查询所用历史，并检查实际行数和编码后的数据量。每次最多 8,192 行、4 MiB，共享 2 秒回调时限。确认信号仅查询当前柱。同步读取在可取消的专用线程执行，子进程退出或回调超时即停止等待。合作式读取器可用 `read_cancellation()` 中止阻塞等待。第三方同步调用如不响应取消，会在自行返回后退出，进程内最多保留 16 个策略 I/O 线程。
- 每次回调使用新生成的 ID 绑定取数、错误及最终决定。宿主通过单独的同步请求确认回调结束，再将决定交给风控。缺失、过期、重复及回调外的回复均导致协议错误。等待行情期间也监测子进程，异常退出会中断事件循环并随账户上下文释放锁。宿主监测绝对 RSS（256 MiB）和策略 CPU（60 秒），CPU 基线取在可信运行时初始化完成、首次读取策略代码之前。
- 逐项先观察失败用例，再运行对应修复后的测试。受影响测试 `PYTHONPATH=$PWD/core/src /Users/dikui/Project/Talaria/.venv/bin/python -m pytest tests/account -q -rs -p no:cacheprovider` 得到 98 通过、5 跳过。`python -m talaria contract-check` 零漂移，`git diff --check` 通过。
- 本环境禁止嵌套 Seatbelt。5 项待在沙箱外 macOS 验证，覆盖未批准回测、依赖重载、冷库与网络拒绝，以及两个模式下外部行情文件和批准库的读写权限。复跑命令如下。

```sh
PYTHONPATH=$PWD/core/src /Users/dikui/Project/Talaria/.venv/bin/python -m pytest tests/account -k 'agent_strategy_can_backtest_without_approval or production_worker or seatbelt_denies' -q -rs -p no:cacheprovider
```

全量执行 `PYTHONPATH=$PWD/core/src /Users/dikui/Project/Talaria/.venv/bin/python -m pytest tests -q -p no:cacheprovider`，986 项通过、15 项失败、1 项初始化错误、5 项跳过、1 项默认取消选择，另有 1 条 Starlette 弃用警告。14 项失败及 1 项初始化错误涉及沙箱禁止监听回环端口，另 1 项失败涉及 DSH 写入沙箱外的配置目录，与 trading/01 中记录的环境限制一致。调试期间曾观察到子进程退出时资源采样先于进程回收失效，已修复该竞态，最终局部与全量运行中的账户用例均通过。
