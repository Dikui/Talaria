# 06 — 架构文档与代码重新对齐

**What to build:** 新加入的人或代理读 `docs/architecture.md` 和 README 时，看到的 ADR 列表、目录结构、模块位置和指标实现说明与当前代码一致。尚未实现的层明确标成“规划中（M3/M4）”，不再写成已经与代码同构。

**Blocked by:** 01、02、03、04、05

**Source of truth:** 当前代码（01–05 合入后）；ADR-0010（纯 Python 指标核）、ADR-0011（快照冻结时发布冷库）。文档与代码冲突时以代码和 ADR 为准。

**Status:** resolved

- [ ] ADR 列表补全到 0011；目录树注释里的 “ADR-0001 .. ADR-0007” 修正为实际范围。
- [ ] 目录树按实际包结构重写：`evaluation/`、`replay/`、`feed/` 的实际归属写清楚；`strategy/`、`portfolio_risk/`、`execution/`、`ledger_journal/`、`orchestration/`、`packs/us_equity/`、`dsh-bridge` 等尚不存在的目录标注“规划中”和对应里程碑；删除“严格同构”这种与现状不符的说法，或改成有条件的表述。
- [ ] §3.1 算子核一段改为以 ADR-0010 为准：纯 Python `Decimal` 实现是唯一权威，TA-Lib / talipp 只是固定版本的候选依赖，运行时不使用。依赖本身保留，不删除。
- [ ] 关键位纯函数算法的路径写成实际的 `foundation/key_levels`；预警、扫描引擎、存储守卫、用量扫描写成 01 之后的位置；市场包注册与 `--pack` 写进 §1.1 / §5.1。
- [ ] §5.2 写明 02、03 之后协同状态库的写线程和只读连接模型。
- [ ] README 的启动命令和数据目录说明与 04 一致。
- [ ] 搜索无残留：`foundation/levels`、“复用 `TA-Lib`（BSD，冷库批量）与 `talipp`”。
