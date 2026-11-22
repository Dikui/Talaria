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
