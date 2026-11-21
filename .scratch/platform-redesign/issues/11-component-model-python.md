# 11 — 组件接口、运行时上下文与 Python 写法

**What to build:** 在 Notebook 里（研究模式，不隔离）用 Python 写一个指标或信号组件，通过 `ctx` 取数并在历史区间上运行，得到的结果与图表上同一指标逐值相等；组件试图读取未收盘柱或未收盘的大周期柱时拿不到数据。

**Blocked by:** 04 — Python 取数 SDK。

**Source of truth:** ADR-0019；`docs/architecture.md` §5.1、§5.2；GLOSSARY 的 Component、Runtime Context。

**Status:** ready-for-agent

- [ ] 指标、信号、策略三类组件接口与 `ComponentMeta`（输入、参数及取值范围、输出类型、版本）。
- [ ] `ctx` 复用 04 的 `as_of` 截断；只在收盘后求值；只暴露已确认的柱（收盘时刻已过但确认未到的柱不可见）；跨周期只暴露最近一根已收盘的大周期柱。
- [ ] 算子注册表：现有 `foundation/operators` 算子注册名称、参数 Schema、版本；注册时必须有黄金向量，缺少时注册失败。
- [ ] 测试：截断式前视审计（任取截断点，截断运行与全区间运行在截断点之前的输出逐值一致）；跨周期非重绘；与图表指标接口结果逐值相等。
