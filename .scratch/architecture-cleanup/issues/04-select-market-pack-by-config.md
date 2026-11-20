# 04 — 启动时按配置选择市场包，场所相关内容全部由市场包提供

**What to build:** 加入一个新市场时，只需在 `packs/` 下注册一个市场包，不用修改感知层、工作台网关或 CLI。`talaria up` 和 `evaluate-levels` 通过 `--pack`（默认 `crypto_perp`）选择市场包。标的清单、历史连接器、实时流、原始存档子目录名、协同提示词第二段的市场领域技能文本，都由市场包提供。对现有用户来说没有任何变化，原有数据目录照常可用。

**Blocked by:** 01 — 把有状态引擎和 I/O 模块移出 L0 foundation

**Source of truth:** ADR-0002（Capability Traits 与市场包）；`docs/architecture.md` §1.1 第 3 条“跨品种零条件分支”、§3.3 提示词三段式组装。

**Status:** ready-for-agent

- [ ] `packs` 提供按名称查找市场包的注册表；名称未知时 CLI 以退出码 2 报出可选值列表。
- [ ] `MarketPack` 协议补充：创建历史连接器、创建实时流、原始存档目录标识、领域技能提示词文本。fixture 连接器和 JSONL 回放仍是与市场无关的通用实现。
- [ ] `workbench_api` 和 `cli` 不再导入任何具体市场包或 Bybit 类；搜索 `CRYPTO_PERP_PACK`、`BybitLinearConnector`、`BybitPublicStream` 只在 `packs/` 和测试里出现。
- [ ] 原始存档路径由市场包提供的目录标识决定；`crypto_perp` 返回 `bybit_linear`，因此现有 `<data_dir>/data/raw/bybit_linear/...` 不需要迁移（加测试锁定这一点）。
- [ ] L0 的会诊提示词组装从参数接收市场领域技能文本；搜索 `USDT`、`永续` 在 `foundation` 和 `perception` 的代码中无残留（注释和文档字符串除外）。
- [ ] 新增测试：注册一个最小的假市场包，用 `--pack` 启动 `create_app`，`/api/instruments` 返回假包的标的，会诊提示词第二段是假包提供的文本。
- [ ] `tests/cli`、`tests/workbench_api`、`tests/perception`、`tests/acceptance` 全部通过；`tests/packs/test_online_bybit.py` 需要真实网络、实现环境跑不了，要同步更新它的构造方式，并在报告中注明没有运行。
