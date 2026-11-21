# 09 — launchd 常驻服务与功能开关

**What to build:** `talaria service install` 后，服务随登录启动、崩溃自动重启，关掉所有页面后 Notebook 仍能取数；`talaria service status` 显示运行状态与已开启的功能；行情、冷库写入、预警、扫描可以按配置单独开关。

**Blocked by:** None — can start immediately.

**Source of truth:** ADR-0014；`docs/architecture.md` §1.1、§8.1。

**Status:** ready-for-agent

- [ ] `install | uninstall | status` 三个子命令；plist 显式设置 `ProcessType` 与 `KeepAlive`，日志写入数据目录。
- [ ] 功能开关配置放在数据目录；关闭的功能不启动对应引擎，也不写对应存储。
- [ ] `talaria up` 前台模式保留。
- [ ] 测试：配置解析与各开关组合下组合根装配的引擎；plist 生成内容快照。launchd 实际安装需在本机手动验证，在验收说明中记录步骤与结果。
