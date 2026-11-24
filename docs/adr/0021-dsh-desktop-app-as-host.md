---
status: superseded by ADR-0023
---

> **已被取代（2026-11）：** 请参阅 [ADR-0023](./0023-standalone-workbench-and-rust-data-engine.md)。日常看盘工作台的桌面宿主已切换为独立 `Tauri v2` 应用（`workbench/src-tauri`），DSH 桌面应用仅作为可选外部协同会诊客户端。

# DSH 桌面应用作为工作台宿主，常驻服务不再托管 DSH

启停分工已由 [ADR-0022](./0022-dsh-desktop-app-owns-service-lifecycle.md) 修订。

2026-11-22 决定：交易员日常用 DSH 桌面应用（`/Applications/DeepSeek Harness.app`，`desktop` profile），Talaria 插件装进这个 profile，图表、预警、扫描、关键位入口出现在桌面应用的侧边栏。常驻服务只负责数据，不再以子进程启动网页版 DSH；它照常写出桥接凭据，插件按安装时记下的路径读取。

本 ADR 修订 ADR-0020 后果第一条（“服务以子进程方式启停 DSH 宿主”）与 platform/03 的做法；ADR-0020 的其余部分不变：DSH 是日常入口，功能面板的数据仍直连服务快通道，服务托管的 `/desk` 页面仍是兜底。

- `desktop` profile 里还有交易员的其他插件与会话。Talaria 插件在这里只增加面板、工具和一个可选的“Talaria 分析副驾驶”预设，不改变 profile 的默认预设、沙箱与审批设置；只读守卫只作用于 Talaria 自己的会话（会诊、简报和选用该预设的会话）。原来 `talaria-desk` profile 的限制性补丁只在那个 profile 里使用。
- 桌面应用的生命周期由应用自己管理；`talaria open` 打开桌面应用，应用不可用时打开兜底页面。
- `talaria up` 的双进程模式保留给开发和自动化测试，不作为日常运行方式。

## Considered Options

- **继续由服务托管网页版 DSH（ADR-0020 原做法）**：交易员已改用桌面应用，两套 DSH 并存会出现两个入口、两份会话；桌面应用安装时也替换了全局 `dsh` 命令。否决。
- **两种都保留，服务托管网页版 DSH 作为可选项**：多一条启停路径要维护，眼下没有需要它的场景。否决。
- **把限制性补丁原样装进 `desktop` profile**：会把交易员所有会话改成只读沙箱并拦截白名单外的工具。否决。

## Consequences

- DSH 版本基线从 `0.2.2-rc.3` 改为桌面应用自带的 `0.2.2-rc.5`；升级桌面应用前先跑插件测试与侧边栏实测。
- `talaria dsh-install` 默认安装到 `desktop` profile，`dsh` 可执行文件找不到时使用桌面应用自带的命令行。
- 插件读取凭据的路径在安装时写入插件配置；服务重启换了 token 后，插件按需重读，不需要重启桌面应用。
