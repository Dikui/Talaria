# 核实并锁定外部依赖版本与许可证

Labels: research
Type: research
Status: ready-for-agent
Blocked by: none

## Question

M1 编码前，需用一手来源（官方仓库、发布页、许可证文件、监管机构公告）核实并锁定以下依赖，结论写入 `docs/research/`：

1. **DeepSeek Harness**：当前稳定版本号、`deepseek-harness-sdk`（`stdio` JSON-RPC）与 `ui-slots` / `typert` 接口是否仍为 0.x 预览、近期破坏性变更；是否提供“每轮结束”宿主钩子（供认知 Token 用量上报，见架构规范 §3.6）。
2. **Optuna**：当前主版本（文档中写的 `v5.1` 是否存在）、`JournalStorage` / `JournalFileBackend` 多进程追加写与 `trial.set_constraint` 的现状。
3. **KLineChart**：v10 是否已正式发布（非 beta），`registerIndicator` 是否支持直接渲染外部传入的计算结果、`setDataLoader` 分页接口现状，许可证。
4. **指标库**：`TA-Lib` C 库许可证（Python 封装为 BSD-2-Clause）与预编译 wheel 覆盖平台；`talipp` 最新发布版本与维护状态（MIT）。
5. **关键位参考项目许可证**：`smart-money-concepts`（目录页显示 MIT，需核对 LICENSE 文件）、`etnfrank/support_resistance`、`trendln`、`market-structure`。
6. **美国 PDT 规则现状**：`< $25,000` 账户滚动 5 交易日日内交易次数限制是否已由 FINRA / SEC 修订，影响 `us_equity` 包 `OrderConstraintTrait`。

产出：每项给出锁定版本或结论、来源链接，以及需要回改架构规范或 ADR 的位置。

## Answer

## Comments
