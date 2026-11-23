# 05 — 服务由 DSH 桌面应用启停，移除 launchd

**What to build:** 交易员打开 DSH 桌面应用，Talaria 服务随之启动；退出应用，服务随之停止；服务崩溃时插件自动重启。不再需要 `talaria service install`，也不再有 launchd 作业。

**Blocked by:** None — can start immediately.

**Source of truth:** ADR-0022（取代 ADR-0014 后台的 launchd 部分）、ADR-0021、ADR-0020。

**Status:** ready-for-agent

## 现状

- 服务由 launchd 作业 `com.talaria.workbench` 启动：`<venv>/bin/talaria up --solo --host 127.0.0.1 --port 8765 --data-dir <dir> --pack crypto_perp`，`WorkingDirectory=<data-dir>`，环境变量带 `HTTP_PROXY`/`HTTPS_PROXY`/`NO_PROXY`，日志写 `<data-dir>/logs/service.{out,err}.log`。生成逻辑在 `core/src/talaria/cli/service.py`，命令在 `core/src/talaria/cli/main.py` 的 `_cmd_service`。
- `talaria dsh-install`（`main.py` `_cmd_dsh_install`）对 `desktop` profile 调用 `dsh_host.install_talaria_patch`，在 `~/.dsh/profiles/desktop/cordis.patch.yml` 的标记段里写插件配置 `{credentialFile, desktop: true}`。
- 插件入口 `dsh/packages/talaria-dsh-plugin/index.js` 的 `apply(ctx, config)`，用 `ctx.effect` 注册可清理的副作用；桥接凭据由服务写到 `<data-dir>/runtime/dsh-bridge.json`，插件按需重读。
- 服务就绪的判断方式见 `_cmd_open`：`probe_lock(data_dir)` 的持有者角色为 service，且 `read_service_descriptor(data_dir)` 的 pid 与之相同。

## 验收

- [ ] **安装写入启动配置**：`dsh-install` 装到 `desktop` profile 时，插件配置新增 `service` 段：`command`（`talaria` 绝对路径 + `up --solo --host 127.0.0.1 --port <port> --data-dir <abs> --pack <pack> --exit-with-parent`）、`cwd`（数据目录）、`env`（安装时环境中的 `HTTP(S)_PROXY`/`ALL_PROXY`/`NO_PROXY` 及小写形式，沿用 `service.py` 现有的代理收集逻辑）、`logDir`。`dsh-install` 新增 `--port`（默认 8765）、`--pack`（默认 `crypto_perp`）、`--program`（默认同 `_default_service_program`）。非 desktop profile 不写 `service` 段。重复执行幂等。
- [ ] **`--exit-with-parent`**：`talaria up` 新增该参数；开启时后台线程阻塞读 stdin，读到 EOF 即走与 SIGTERM 相同的优雅退出路径（释放锁、删除桥接凭据与描述文件）。
- [ ] **插件托管**：`config.service` 存在时，插件在 `ctx.effect` 里启动一个监管器：
  - 启动前探测：数据目录已有健康服务（锁由 service 角色持有且描述文件 pid 一致，可用 Node 读 `runtime/` 下的描述文件 + 检查 pid 存活 + 请求健康接口，具体以 Python 侧描述文件格式为准）且不是自己起的，就不启动，每 5 秒复查一次，对方退出后接管。
  - 启动：`child_process.spawn(command[0], command.slice(1), { cwd, env: {...process.env, ...env}, stdio: ['pipe', out, err] })`，stdout/stderr 追加写入 `logDir/service.out.log`、`service.err.log`。
  - 崩溃重启：非主动停止的退出按 1s 起、翻倍、上限 60s 退避重启；连续运行满 60s 后退避归零。
  - 清理（effect 返回的 dispose）：关闭 stdin，发 SIGTERM，10 秒内未退出发 SIGKILL；返回的 Promise 等待子进程退出。dispose 后不再重启。
  - 监管器写成独立模块（如 `service-supervisor.js`），spawn、计时器、探测都可注入，便于单测。打包清单 `build_install_bundle` 的 `runtime_files` 同步加入。
- [ ] **launchd 退役**：移除 `talaria service install` 子命令及只为它服务的代码与测试；保留 `service uninstall`。`service status` 去掉 plist/launchd 行，改为显示：插件是否装进 desktop profile 且含 `service` 段、桌面应用是否运行、数据目录锁持有者与写入接口；若 `~/Library/LaunchAgents/<label>.plist` 仍存在，提示运行 `talaria service uninstall`。`dsh-install` 检测到该 plist 时同样打印提示（不自动卸载）。`_launchd_data_dirs` 的回放保护改为同时保护插件配置里的数据目录（读不到就跳过）。
- [ ] **`talaria open`**：已安装桌面应用时直接 `open -a "DeepSeek Harness"`，不再等待服务就绪；未安装时仍需服务就绪，打开 `/desk`，服务未运行时提示 `talaria up --solo`，退出码非 0。`--print` 行为保留。
- [ ] **文档**：README“启动工作台”一节改为 `dsh-install` + `open`，删掉 `service install`；说明应用关闭时数据不采集、迁移时先 `talaria service uninstall`。`docs/architecture.md` 中描述 launchd 常驻的段落同步修改。ADR-0014 顶部 status 标注“后台 launchd 部分已被 ADR-0022 取代”，ADR-0021 加“启停分工已由 ADR-0022 修订”。
- [ ] **测试**：插件监管器单测（探测到外部服务不启动、崩溃退避、dispose 先 SIGTERM 再 SIGKILL、dispose 后不重启）；CLI 测试覆盖 `dsh-install` 写出 `service` 段、`--exit-with-parent` 读到 EOF 退出、`open` 两种情况、`service status` 的 plist 残留提示。只跑受影响的测试：`pytest tests/<相关模块>` 与 `pnpm -C dsh --filter @talaria/dsh-plugin test`。
- [ ] 真机验证（由 Claude 做，不在 Codex 范围）：卸载 launchd 作业、重新 `dsh-install`、重启桌面应用，确认服务被拉起；退出应用后服务停止。

## Comments
