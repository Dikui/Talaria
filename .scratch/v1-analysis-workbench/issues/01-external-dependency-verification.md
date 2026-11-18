# 核实并锁定外部依赖版本与许可证

Labels: research
Type: research
Status: ready-for-agent
Blocked by: none

## Question

M1 编码前，需用一手来源（官方仓库、发布页、许可证文件）核实并锁定以下依赖，结论写入 `docs/research/`：

1. **DeepSeek Harness**：当前稳定版本号、`deepseek-harness-sdk`（`stdio` JSON-RPC）与 `ui-slots` / `typert` 接口是否仍为 0.x 预览、近期破坏性变更；是否提供“每轮结束”宿主钩子（供认知 Token 用量上报，见架构规范 §3.6）；单调执行守卫与 `dsh --dump-config` 的现状。
2. **KLineChart**：v10 是否已正式发布（非 beta），`registerIndicator` 是否支持直接渲染外部传入的计算结果、`setDataLoader` 分页接口现状，许可证。
3. **指标库**：`TA-Lib` C 库许可证（Python 封装为 BSD-2-Clause）与预编译 wheel 覆盖平台；`talipp` 最新发布版本与维护状态（MIT）。
4. **关键位参考项目许可证**：`smart-money-concepts`（目录页显示 MIT，需核对 LICENSE 文件）、`etnfrank/support_resistance`、`trendln`、`market-structure`。

产出：每项给出锁定版本或结论、来源链接，以及需要回改架构规范或 ADR 的位置。第 1 项结论阻塞 09；第 2、3 项结论阻塞 02、04；第 4 项结论阻塞 10。

## Answer

## Comments

- 2026-11-18：原第 2 项（Optuna `v5.1`、`JournalStorage`、`trial.set_constraint`）与第 6 项（美国 PDT 规则现状）属于 M4 范围，与本需求无关，已移出；编写 M4 需求时重新列入调研。
