# 04 — Talaria 装进 DSH 桌面应用，服务不再托管 DSH

**What to build:** 交易员打开 DSH 桌面应用，侧边栏里就有图表、预警、扫描、关键位四个入口，点开即是 platform/02 做好的面板；会诊、简报和分析工具照常可用。常驻服务只跑数据并写出桥接凭据；`talaria open` 打开桌面应用。装进 `desktop` profile 后，交易员在桌面应用里的其他会话行为不变。

**Blocked by:** None — can start immediately.

**Source of truth:** ADR-0021（修订 ADR-0020 后果第一条）、ADR-0020、ADR-0005；`docs/architecture.md` §1.1、§8.1。

**Status:** ready-for-agent

## 现状

- 桌面应用：`/Applications/DeepSeek Harness.app`，版本 0.2.2-rc.5，profile 目录 `~/.dsh/profiles/desktop`（已装有交易员的第三方插件，如 `dsh-better-sidebar`、`dsh-context`）。自带命令行：`/Applications/DeepSeek Harness.app/Contents/Resources/runtime/cli/bin/dsh`。全局 `dsh` 命令已不存在。
- `talaria dsh-install` 默认装到 `talaria-desk` profile，并要求 PATH 里有 `dsh`。
- 插件 `dsh/packages/talaria-dsh-plugin` 的 `package.json` 声明了 `dsh.bundle.patch`（`talaria-desk.patch.yml`：只读沙箱、审批、默认预设改为 talaria-desk）；`index.js` 用 `ctx.tools.guard` 全局注册只读守卫。装进 `desktop` profile 会改掉交易员所有会话。
- 插件经 `bridge.js` 读取凭据：`TALARIA_DSH_BRIDGE` 环境变量、`cwd/.talaria/runtime/dsh-bridge.json`、`~/.talaria/runtime/dsh-bridge.json`。桥接文件只在服务托管 DSH 时写出。
- platform/03 让 launchd 服务托管 DSH 子进程（`cli/dsh_host.py`、`cli/service.py`）。

## 验收

- [ ] 安装：`talaria dsh-install` 默认装到 `desktop` profile（`--dsh-profile` 可改）；`dsh` 按 `--dsh-bin`、`TALARIA_DSH_BIN`、PATH、桌面应用自带命令行的顺序查找。安装时把凭据文件的绝对路径（`<data-dir>/runtime/dsh-bridge.json`，数据目录按 `talaria open` 的解析顺序）写进插件配置。重复执行幂等。
- [ ] 不改交易员的 profile 设置：装进 `desktop` profile 后，profile 的默认预设、沙箱与审批设置不变（`dsh --profile desktop --dump-config` 前后对比，只多出 Talaria 插件与一个非默认的“Talaria 分析副驾驶”预设）。限制性补丁只在 `talaria-desk` profile 生效。
- [ ] 守卫只作用于 Talaria 会话：会诊（`talaria-consult-` 前缀）、简报（`talaria-briefing-` 前缀）以及选用 Talaria 预设的会话受只读守卫约束；其他会话的工具调用不受影响。补测试覆盖两种会话。
- [ ] 凭据：常驻服务无论是否托管 DSH 都写出 `dsh-bridge.json`（0600），退出时删除；插件每次调用前检查文件是否变化（mtime 或内容），服务重启换 token 后无需重启桌面应用即可恢复；凭据缺失或服务未运行时，面板显示清楚的离线说明，不抛未处理异常。
- [ ] 服务：`talaria service install` 不再托管 DSH（去掉 `--dsh-bin`、`DSH_HOME`、`PATH` 等 DSH 相关 plist 参数，`--solo` 成为唯一行为后移除该参数或保留为无操作的兼容参数）；`service status` 的 DSH 一行改为显示桌面应用是否安装、是否运行，以及 Talaria 插件是否在其 profile 中。`talaria up` 的双进程模式保留给开发与测试。
- [ ] `talaria open`：服务在运行时打开桌面应用（`open -a "DeepSeek Harness"`）；桌面应用未安装时打开兜底页面 `<base_url>/desk` 并说明；服务未运行时提示启动命令，退出码非 0。`--print` 打印将要执行的动作。
- [ ] 侧边栏：在 rc.5 桌面应用中，四个入口默认出现在侧边栏（不在 More 折叠里），与已装的 `dsh-better-sidebar` 共存。以 rc.5 自带的客户端包源码与类型为准（`/Applications/DeepSeek Harness.app/Contents/Resources/` 下查找 `dsh-client-ui-layout`、`dsh-client-ui-sidebar`），不要猜接口。
- [ ] 文档：README 启动说明、`docs/architecture.md` §1.1 与 §8.1、ADR-0020 后果第一条加“已由 ADR-0021 修订”的标注；DSH 版本基线改为 0.2.2-rc.5。
- [ ] 测试：插件测试覆盖守卫作用范围、凭据重读、安装后的插件配置；CLI 测试覆盖 `dsh-install` 的查找顺序与配置写入（不执行真实 `dsh`）、`service install` 的 plist 不含 DSH 参数、`open` 三种情况。真实桌面应用侧边栏与 profile 设置对比由人工在本机验证，步骤写进实现记录。
