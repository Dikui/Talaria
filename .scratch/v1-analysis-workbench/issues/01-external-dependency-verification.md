# 核实并锁定外部依赖版本与许可证

Labels: research
Type: research
Status: resolved
Blocked by: none

## Question

M1 编码前，需用一手来源（官方仓库、发布页、许可证文件）核实并锁定以下依赖，结论写入 `docs/research/`：

1. **DeepSeek Harness**：当前稳定版本号、`deepseek-harness-sdk`（`stdio` JSON-RPC）与 `ui-slots` / `typert` 接口是否仍为 0.x 预览、近期破坏性变更；是否提供“每轮结束”宿主钩子（供认知 Token 用量上报，见架构规范 §3.6）；单调执行守卫与 `dsh --dump-config` 的现状。
2. **KLineChart**：v10 是否已正式发布（非 beta），`registerIndicator` 是否支持直接渲染外部传入的计算结果、`setDataLoader` 分页接口现状，许可证。
3. **指标库**：`TA-Lib` C 库许可证（Python 封装为 BSD-2-Clause）与预编译 wheel 覆盖平台；`talipp` 最新发布版本与维护状态（MIT）。
4. **关键位参考项目许可证**：`smart-money-concepts`（目录页显示 MIT，需核对 LICENSE 文件）、`etnfrank/support_resistance`、`trendln`、`market-structure`。

产出：每项给出锁定版本或结论、来源链接，以及需要回改架构规范或 ADR 的位置。第 1 项结论阻塞 09；第 2、3 项结论阻塞 02、04；第 4 项结论阻塞 10。

## Answer

结论（证据与约束见 `docs/research/external-dependency-verification-v1.md`）：

1. **DeepSeek Harness：无 stable release**。最新 `0.2.2-rc.5`（2026-11-18，prerelease）；本地锁定基线 `0.2.2-rc.3`；`deepseek-harness-sdk` 最新 `0.2.2rc2`，ui-slots / typert 仍为 0.x 预览。**每轮结束钩子存在（`session/event` 的 `turn/end`），但事件不含 usage**，需折叠 attempt 事件；`deriveTurnTokenUsage` 非公开导出。单调守卫存在且单调（`ctx.tools.guard`），但 `dsh --dump-config` 不能快照守卫（仅能快照权限/插件配置）。`--dump-config` 必须带 `--profile`，输出为含 `!!js` 的 YAML 风格文本；`--dump-config-schema` 在 rc.3 退出码为 1。**结论：不阻塞 09**，但需按上述约束实现并加行为测试。
2. **KLineChart `10.1.1` 正式版**（2026-11-16，非 beta/rc），Apache-2.0。`registerIndicator` 不能直接注入外部 `result`；通过注册模板 + `createIndicator({name, calc})` 的 `calc` 返回外部数组渲染（`calc` 必须与 `dataList` 等长同序；更新时换新闭包并 `overrideIndicator`）。`setDataLoader` v10 分页可用（`init/forward/backward/update` + `callback(data, more)`）。**不阻塞 02。**
3. **TA-Lib `0.8.3`**：C 库 BSD-3-Clause、Python 封装 BSD-2-Clause；有 `cp314` + `macosx_14_0_arm64` wheel，Python 3.14.6 实测可用，**无需纯 Python/numpy 回退**（若支持 <macOS 14 或非 arm64 再评估）。**talipp `2.7.0` MIT**，低频维护（2025-09 最后 push）。**不阻塞 04。**
4. **许可证**：`smart-money-concepts` MIT、`trendln` MIT、`market-structure`（`fortunato/pymarket-structure`）MIT；三者仅可按洁净室借鉴，不直接复制。`etnfrank/support_resistance` 根目录无 LICENSE 文件，仅 `setup.py` 声明 `wtfpl`，且停更于 2019，**标记不可复制**。**不阻塞 10**，但实现需记录参考 commit 与洁净室流程。

需回改位置（本轮只记录，未改）：`docs/architecture.md:403`（守卫快照断言）、`docs/architecture.md:310`（用量折叠与幂等）、`docs/architecture.md:377`/`:380`（SDK 版本与 `dsh_bin`）；ADR-0001 / ADR-0005 / ADR-0008 仅需版本校准。

## Comments

- 2026-11-18：原第 2 项（Optuna `v5.1`、`JournalStorage`、`trial.set_constraint`）与第 6 项（美国 PDT 规则现状）属于 M4 范围，与本需求无关，已移出；编写 M4 需求时重新列入调研。
- 2026-11-19：完成外部依赖核实并提交报告 `docs/research/external-dependency-verification-v1.md`。实测 `dsh 0.2.2-rc.3`（无 stable，最新 rc.5）；`--dump-config` 必须带 `--profile`，单调守卫无法从 dump 证明；SDK `0.2.2rc2` 与本地 rc.3 仅完成 initialize/close 握手；KLineChart `10.1.1` 外部结果经 `calc` 渲染、`setDataLoader` 分页可用；TA-Lib `0.8.3`（cp314 arm64 wheel）与 talipp `2.7.0` 在 Python 3.14.6 实测可用；`support_resistance` 无 LICENSE 文件，标记不可复制。未改架构/ADR，仅记录回改位置。
