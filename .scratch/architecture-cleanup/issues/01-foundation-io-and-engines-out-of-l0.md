# 01 — 把有状态引擎和 I/O 模块移出 L0 foundation

**What to build:** 维护者打开 `foundation` 时，看到的只有契约、Decimal / 时点规则、能力特征协议和纯函数算子（含关键位纯函数算法），和 architecture.md §3.1 描述的一致。带运行时状态的 `AlertEngine`、`ScannerEngine` 归入 L1 感知层（architecture.md §3.2 第 3 条）；SQLite 单写者守卫、契约 Schema 导出写盘、DSH 轨迹用量扫描等有 I/O 的模块移出 L0，放到实际使用它们的层（感知层存储 / CLI / 工作台网关）。这是纯搬迁，行为不变。另加一道导入边界测试，防止 I/O 以后再流回 L0。

**Blocked by:** None — can start immediately

**Source of truth:** `docs/architecture.md` §1.1、§3.1、§3.2；ADR-0001、ADR-0008。冲突时以 architecture.md 为准。

**Status:** resolved

- [ ] `AlertEngine`、`ScannerEngine` 及其路由 / 运行时辅助代码移到 L1；预警、扫描的契约模型（`*contracts`）继续留在 L0，因为前后端和 Schema 导出共用它们。
- [ ] SQLite 单写者守卫、契约导出写盘、轨迹用量扫描三个 I/O 模块移出 `foundation`，新位置的导入方向符合 §1.1（L1 不导入 O2；O2 可以导入 L1）。
- [ ] 关键位纯函数引擎、指标算子核、提示词组装（纯字符串函数）留在 L0，不搬。
- [ ] 新增测试：遍历 `talaria.foundation` 下所有模块的 import，`sqlite3`、`duckdb`、`pyarrow`、`websockets`、`httpx`、`compression`、`asyncio` 以及 `talaria.perception` / `feed` / `workbench_api` / `packs` / `cli` 都不得出现；违规时报出模块名。
- [ ] 搜索无残留：`talaria.foundation.alerts.engine`、`talaria.foundation.scanner.engine`、`talaria.foundation.scanner.routing`、`talaria.foundation.sqlite`、`talaria.foundation.contracts_io`、`talaria.foundation.collaboration.usage_scan` 在 `core/` 和 `tests/` 里都不再出现。
- [ ] `talaria contract-check` 零漂移；生成的 JSON Schema 和 TS 类型字节级不变。
- [ ] 受影响的测试目录（`tests/foundation`、`tests/perception`、`tests/workbench_api`、`tests/cli`、`tests/acceptance`）全部通过；测试文件跟随模块搬到对应层的测试目录。
