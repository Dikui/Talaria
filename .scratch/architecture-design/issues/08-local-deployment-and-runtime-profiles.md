# 确定单机轻量化进程拓扑、运行态配置与数据库分工

Labels: wayfinder:grilling
Type: grilling
Status: resolved
Blocked by: 04, 05, 06, 07

## Question

在“单机零重型中间件、日常维护轻便”的反目标约束下，综合前序各层设计，全系统的物理部署与运行态管理应如何定型？具体需敲定：
1. 日常实时双轨看盘态（`Shadow Run`）、历史批量回放态（`Historical Replay`）与多进程 `Optuna` 参数寻优态下的进程拓扑与启停方式；
2. 本地存储文件布局与并发锁隔离方案（如何划分时点清单库、多子账户账本库、画线/预警/复盘工作台状态库与 `Optuna` `JournalStorage`，规避 SQLite 单写者锁竞争）；
3. 模块异常降级策略（如外部行情断流、DSH 模型超时）与跨缝隙自动化契约回归检查清单（含 DSH `--dump-config` 与 JSON Schema 子集校验）。

## Answer

按推荐方案定型单机零中间件进程拓扑、按写职责分库隔离布局与异常降级及契约回归防线：

### 1. 三类运行态的单机极简进程拓扑与一键启停

全系统零 Docker 集群、零 Kafka/Redis、零外部数据库服务，仅靠 `uv`（Python）与 `pnpm`（Node/DSH）本地运行，通过 `talaria` CLI 统一管理三种运行态：

1. **日常实时双轨看盘态（`Shadow Run` —— `talaria up`）**：
   - **进程 1（核心必选）**：`talaria-daemon`（单个 Python `asyncio` 进程，监听 `127.0.0.1:8765` 并校验会话级本地随机 Bearer Token 防浏览器跨站请求伪造），内部协作运行实时数据源 WebSocket 连接器、热流窗口、毫秒级 `AlertEngine / Scanner`、多子账户 `PositionExecutor` 模拟撮合、复式账本落盘及本地快通道 HTTP/WS 网关，并内置静态路由 `http://127.0.0.1:8765/desk` 直接托管纯前端看盘包 `talaria-desk-ui`。
   - **进程 2（双轨协同默认同启，可通过 `--solo` 跳过）**：`dsh web --profile talaria-desk`（默认监听 `127.0.0.1:3080`），挂载 `talaria-desk-ui` 插槽与智能体对话面板，通过 `dsh-bridge` 连接 `127.0.0.1:8765`。`talaria up` 负责父子进程健康探针、日志分流与 `SIGINT/SIGTERM` 优雅排空退出（退出前刷盘冷库增量与账本 WAL 检查点）。
2. **历史批量回放与归因态（`Historical Replay` —— `talaria replay`）**：
   - 纯代码量化回放由单个 Python CLI 进程推进确定性虚拟时钟，使用独立运行目录 `runs/<run_id>/ledger.sqlite`（或内存库导出）全速执行；
   - 包含智能体策略的历史回放由 Python `ReplayRunner` 通过 `deepseek-harness-sdk`（`stdio` JSON-RPC 2.0）按需驱动无网络、无 `shell`、无写文件工具的受限 `talaria-eval`（基于 `sdk-minimal`）子进程池。
3. **多进程防过拟合参数寻优态（`Optuna Study` —— `talaria optimize --workers N`）**：
   - 主进程先执行单次截断防前视审计与预热漂移审计；通过后派生 $N$ 个本地 Python Worker 进程。各 Worker 以只读方式共享 `data/lake/*.parquet` 冷库，在各自进程内存 `:memory:` 中运行独立 `SimExecutionAdapter` 与临时账本，试验结果追加写入支持多进程并发文件锁的 `Optuna JournalStorage`，彻底避开多进程抢写同一 SQLite 账本库；开发复盘期可按需执行 `talaria study-ui` 启动只读 `optuna-dashboard` 或在工作台复盘页查看帕累托前沿与邻域平坦度图。

### 2. 本地存储物理分库与单写者免锁隔离方案

所有持久化文件统一收纳于 `$TALARIA_DATA_DIR`（默认仓库外或本地忽略目录 `~/.talaria/` 或 `./var/`），按**唯一独占写者**物理拆分为 4 个独立 SQLite 库（统一开启 `PRAGMA journal_mode=WAL; synchronous=NORMAL/FULL; busy_timeout=5000`）与 3 类不可变/追加文件区，从物理上消除跨模块写锁竞争：

```text
$TALARIA_DATA_DIR/
├── data/
│   ├── raw/                        # 原始接口报文与校验哈希（仅数据采集发布器追加）
│   ├── lake/                       # 按 dataset/feed/timeframe/YYYY-MM 组织的不可变 Parquet 批次
│   └── data_manifest.sqlite        # 【库 1：数据发布器单写者】数据集版本、快照清单、CatalystCalendar、抓取游标
├── state/
│   ├── workspace_state.sqlite      # 【库 2：快通道网关单写者】Watchlist、AlertRule、OverlayRecord、StagedPlanDraft、人工复盘批注
│   ├── trading_ledger.sqlite       # 【库 3：账本协程单写者，synchronous=FULL】Shadow Run 多子账户复式分录、持仓与闭合归因档案
│   └── episodic_memory.sqlite      # 【库 4：盘后归档单写者】仅存已平仓结算经验摘要（强制携带 resolved_ts 防回测穿越）
├── runs/
│   └── <replay_run_id>/ledger.sqlite   # 历史回放每次运行独立分库，绝不与实时影子账本混写
└── studies/
    ├── study_registry.sqlite       # Study 元数据、三段切分边界、累计总试验数与候选晋升审计记录
    └── <study_id>/journal.log      # Optuna JournalFileBackend 多进程安全追加日志
```
*(注：DeepSeek Harness 自身的压缩会话轨迹独立存放于 `$DSH_HOME/profiles/`，由 `TradeJournalRecord` 通过只读路径与 `turn_id` 引用。)*

### 3. 模块异常降级策略与跨缝隙自动化契约回归清单

1. **分级故障隔离与优雅降级**：
   - **外部行情源断流或延迟超限**：`HotStreamBuffer` 按市场时段监控心跳与序列连续性；一旦某标的或数据源断流超过阈值（如加密永续 `> 5s`、美股交易时段超时），立即将该数据源标为 `STALE`：① 暂停该标的 `Alert / Scanner` 新开仓唤醒并在前端顶栏高亮提示断流时长；② 策略评估遇到 `STALE` 快照强制返回 `Abstain`；③ 持续断流超过安全窗口的子账户自动降级为 `REDUCING`（禁开新仓），直至重连完成缺口回补校验。
   - **DeepSeek Harness 宿主离线、沙箱异常或模型推理超时**：① 策略运行器在本地超时墙到达时立即中断等待并产出 `Abstain(MODEL_TIMEOUT)`；② 因采用双通道物理分流与本地自治执行，**人工看盘、画线、盘面预警弹窗、手动模拟开平仓以及存量持仓的四重保护屏障盯盘完全不受任何影响**。
2. **跨缝隙自动化契约回归检查清单（纳入测试门禁）**：
   - **① 跨语言契约同步校验**：断言 Python `foundation` 导出的 `contracts/*.schema.json` 与 `dsh/packages/talaria-contracts` 生成代码完全同步，且所有金额/价格字段类型为十进制字符串格式。
   - **② DSH 工具 Schema 子集门禁**：对所有注册进 `dsh-bridge` 的工具输入输出 Schema 运行 `assertSupportedJsonSchema` 等价规则校验（零根级 `$schema`、零未展开 `$defs`、无不支持关键字）。
   - **③ DSH Profile 配置快照审计**：自动比对 `dsh --profile talaria-desk --dump-config` 与 `dsh --profile talaria-eval --dump-config` 快照，防 `cordis.patch.yml` 整行覆盖特性意外重开 `talaria-eval` 的 `shell`/网络/写文件权限或摘除单调执行守卫（拦截历史轨迹 `fork` 重放提交）。
   - **④ 三端算子精度回归**：用 `contracts/operator_vectors/` 黄金样例同时测试 Python L0 算子、前端图表自定义指标与 PTC 算子绑定，断言前导 `null` 长度与数值误差 `< 1e-8`。
   - **⑤ 本地端口与鉴权基线校验**：断言 `talaria-daemon` 与 `talaria-desk` 默认仅绑定 `127.0.0.1` 回环网卡，且写操作接口未携带启动随机生成的本地会话 Token 时一律返回 `401`。


## Comments

### 交接前整体复查修订（覆盖已解决 5、8 张）
- **原因**：独立复查指出 SQLite 库计数（正文写 4 个、目录列 5 个）与单写者归属表述需对齐真实后台异步写路径，并配合智能体双宿主方案校准进程拓扑。
- **修订结论**：
  1. **精确校准为 5 个职责正交的本地 SQLite 库 + `Optuna JournalStorage`**，按进程内唯一写者 Actor 严格隔离：
     - `data/data_manifest.sqlite`（冷库发布器单写者）；
     - `state/runtime_snapshots.sqlite` + `state/hot_snapshots/*.parquet`（实时冻结快照协调器单协程写者）；
     - `state/collaboration_state.sqlite`（`talaria-daemon` 内统一 `StateWriterQueue` 单协程顺序写者，集中托管 `Watchlist`、`AlertRule` 状态推进、`OverlayRecord`、`StagedPlanDraft`、`ControllerPatch` 与人工复盘批注；外圈快通道、`dsh-bridge` 与后台 `AlertEngine` 均通过内存队列投递命令而非并发抢锁直写库文件）；
     - `state/trading_ledger.sqlite`（账本单协程写者，`synchronous=FULL`，含复式分录与全量认知 Token 成本流水；回放按 `runs/<run_id>/` 隔离）；
     - `studies/study_registry.sqlite`（仅由 `talaria optimize` 主进程在寻优前后单写登记切分边界与汇总总 `n_trials`，多进程运行期各 Worker 仅追加写 `studies/<study_id>/journal.log`）。
     *(盘后已平仓经验索引并入 `collaboration_state.sqlite` 的独立只追加表并强制校验 `resolved_ts <= cutoff_ts`，无需再额外拆第 6 个库。)*
  2. **实时态智能体双通道对齐**：`talaria-daemon` 按需通过 `deepseek-harness-sdk`（`stdio`）托管智能体自治子账户 `talaria-auto` 子进程，与面向人类浏览器的 `dsh web --profile talaria-desk` 职责分离；契约回归门禁同步覆盖 `talaria-desk`、`talaria-auto` 与 `talaria-eval` 三个 Profile 的 `--dump-config` 权限快照。
