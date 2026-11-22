# 03 — 常驻服务托管 DSH 宿主，`talaria open` 打开工作台

**What to build:** `talaria service install` 之后，登录即可用 DSH 工作台：常驻服务以子进程方式启动 DSH 宿主，DSH 崩溃后自动重启，服务本身不受影响。交易员执行 `talaria open`，浏览器直接打开带 token 的 DSH 入口；DSH 不可用时打开服务托管的兜底页面并说明原因。

**Blocked by:** None — can start immediately（与 `platform/02` 改的文件不重叠，可并行）。

**Source of truth:** ADR-0020（后果第一条）、ADR-0014 常驻服务部分；`docs/architecture.md` §8.1。

**Status:** ready-for-agent

## 现状

- `platform/01` 的 launchd Agent 固定跑 `talaria up --solo`（`core/src/talaria/cli/service.py` 的 `install_service`），服务不托管 DSH。
- 前台 `talaria up`（双进程）已经用 `cli/dsh_host.py` 的 `DshHost` 启停 DSH：读 stdout 拿带 token 的入口 URL，崩溃只标记 `offline`，不重启。入口 token 只留在内存里，API 也不暴露它。
- launchd 启动的进程 `PATH` 通常不含 `/opt/homebrew/bin`，按名字找 `dsh` 会失败。

## 验收

- [ ] `service install` 默认安装双进程模式（不再带 `--solo`），`--solo` 作为可选参数保留。安装时解析 `dsh` 的绝对路径（`--dsh-bin` / `TALARIA_DSH_BIN` / `PATH`）写进 plist 参数；找不到时安装失败并提示，或在用户给了 `--solo` 时照常安装。plist 显式写入 `DSH_HOME`（取安装时的值，缺省 `~/.dsh`）。
- [ ] DSH 崩溃后由 `DshHost` 自动重启，指数退避（如 1s 起、上限 60s），连续失败期间状态为 `offline` 并记录最近一次失败原因；重启成功后恢复 `online`，桥接描述文件照常可用。服务退出时先停 DSH。快通道在整个过程中不受影响（现有 `test_up_dual_keeps_fast_path_*` 继续通过）。
- [ ] DSH 每次上线后，把入口 URL（含 token）写入 `<data-dir>/runtime/dsh-entry.json`，权限 0600，目录 0700；DSH 下线或服务退出时删除。日志和 API 里的 URL 仍按现有规则打码。
- [ ] `talaria open [--data-dir]`：
  - 服务在运行且 DSH `online` → 用系统默认浏览器打开 `dsh-entry.json` 中的 URL。
  - 服务在运行但 DSH 不在线 → 打开兜底页面 `<base_url>/desk`，在终端说明 DSH 状态与日志路径。
  - 服务没运行 → 不启动服务，提示用 `talaria service install` 或 `talaria up`，退出码非 0。
  - `--print` 只打印将要打开的地址（token 打码）不打开浏览器，供脚本和测试使用。
- [ ] `service status` 增加一行 DSH 宿主状态（`online` / `starting` / `offline` / 未启用）、pid 与日志路径。
- [ ] 文档：`README.md` 的启动说明和 `docs/architecture.md` §8.1 运行方式改为“`service install` 后用 `talaria open`”，`--solo` 写成可选。
- [ ] 测试：plist 快照（默认双进程、`--solo`、`dsh` 绝对路径与 `DSH_HOME`）；用 `tests/fixtures/m2/fake_dsh_host.py` 模拟 DSH 崩溃后重启与退避；`dsh-entry.json` 的权限、写入与删除时机；`open` 三种情况下的目标地址（用 `--print`）。真实 launchd 安装、登录自启与 `open` 打开浏览器在本机手动验证，在验收说明中记录步骤与结果。
