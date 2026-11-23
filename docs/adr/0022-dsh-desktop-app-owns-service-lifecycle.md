---
status: accepted
---

# 常驻服务改由 DSH 桌面应用启停，不再用 launchd

2026-11-23 决定：Talaria 服务的生命周期交给 DSH 桌面应用里的 Talaria 插件。桌面应用启动并加载插件时，插件以子进程方式启动 `talaria up --solo`；应用退出时停掉服务；服务崩溃时插件按退避重启。launchd 用户级 Agent 不再使用。

本 ADR 取代 ADR-0014 的“后台”一条中“由 launchd 用户级 Agent 负责登录启动和崩溃重启”，以及其 Considered Options 中对“桌面应用托管 Python 服务”的否决；修订 ADR-0021 中“常驻服务只负责数据，不再以子进程启动网页版 DSH”之外的启停分工。其余不变：同一数据目录仍只有一个服务实例（数据目录排他锁），功能面板仍直连服务快通道（ADR-0005、ADR-0020），`/desk` 兜底页面仍由服务托管。

- 启动参数在 `talaria dsh-install` 时写进插件配置：`talaria` 可执行文件绝对路径、端口、数据目录、市场包，以及安装时的代理环境变量。插件不自己拼路径。
- 插件启动前先探测数据目录：已有健康的服务（例如开发时手动 `talaria up`）就直接连它，不再另起；它退出后插件再接管。
- 服务以 `--exit-with-parent` 运行，标准输入关闭即优雅退出，桌面应用被强杀时服务不会变成孤儿进程。
- 日志仍写 `<data-dir>/logs/service.out.log` 与 `service.err.log`。

## Considered Options

- **维持 launchd 常驻（ADR-0014）**：关掉桌面应用后数据照常采集，但交易员要同时理解两套启停（macOS 与 DSH），状态排查也要看两处。交易员选择只由 DSH 管。否决。
- **launchd 常驻，插件只负责检查和拉起**：数据不断，但生命周期仍归 macOS，与“交给 DSH 管”的要求不符。否决。
- **插件以脱离父进程的方式启动服务，应用退出后继续运行**：等于没有管理者，崩溃没人重启，下次启动还要处理残留进程。否决。

## Consequences

- 桌面应用没运行时，行情采集、冷库连续写入、预警和扫描都停止，冷库会出现缺口，回填在下次启动时补；Notebook 与 SDK 在应用关闭时取不到服务尾部数据，只能读已落盘的冷库。
- DSH 桌面应用崩溃或升级出问题时服务一起停；兜底改为手动运行 `talaria up --solo`，再打开 `/desk`。
- `talaria service install` 移除；`talaria service uninstall` 保留用于迁移；`talaria service status` 改为报告插件托管的服务状态，并在仍装有 launchd 作业时提示卸载。
- `talaria open` 只负责打开桌面应用，不再要求服务先就绪。
