# 01 — 冷库数值格式迁移到按列精度的 decimal128

**What to build:** 冷库里的 K 线不再以十进制字符串存储，改为每个标的、每列按自身 scale 存 `decimal128(18, s)`（物理 INT64），并补齐时点列与修订列。已有冷库数据可以用一条命令一次性迁移，迁移后快照、图表、关键位、`evaluate-levels` 读到的数值与迁移前逐值相等。衍生品进入冷库在 08 做。

**Blocked by:** None — can start immediately.

**Source of truth:** ADR-0017、ADR-0018（修订与时点）；`docs/architecture.md` §4.2。

**Status:** resolved

- [x] scale 按列：价格类取价格精度，数量类取数量精度，`turnover` 取两者之和，费率类固定 8；各列 scale 与 `InstrumentSpec` 版本写入 Parquet 元数据，读取方按元数据还原，不假设全库同一 scale。
- [x] 小数位超出列 scale 或超出 18 位有效数字时整批拒绝并报错，不静默舍入。
- [x] 列包含 `ts_event`、OHLCV、`turnover`、`ts_available`、`ts_ingested`、`ts_committed`（int64 毫秒）、`precision`（字典编码）、`revision`；同一 `ts_event` 的不同修订各占一行。
- [x] 读取实现只有一份：先按 `ts_available <= as_of` 截断，再对每个 `ts_event` 取最大 `revision`；省略 `as_of` 取最新。
- [x] 提供一次性迁移命令：读旧字符串 generation，写新格式，清单原子切换；迁移可重复执行（幂等），失败不破坏旧数据。
- [x] 测试：同一批柱迁移前后读出的 `Decimal` 逐值相等，含仓库样本中成交额 `809038.73`；标的改精度后新旧分区混读正确；超出 scale 或 18 位的值被拒绝；同一柱两个修订在两个 `as_of` 下分别返回各自版本。
- [x] 快照构建与现有冷库读取路径的测试保持通过。

## Comments

### 实现记录（data-access/01）

**已完成**

- 冷库数值列改为按列 `decimal128(18, s)`，物理 Parquet 类型为 `INT64`（`store_decimal_as_integer=True`）；价格列取 `price_precision`，`volume` 取 `qty_precision`，`turnover` 取两者之和。文件元数据写入格式标记、逐列 scale、`InstrumentSpec` 版本（对 `tick_size`/`qty_step`/`price_precision`/`qty_precision` 的稳定 JSON 求 sha256 前 12 位）。读取按文件元数据校验，不假设全库同一 scale。
- 写入前整批校验：小数位超出列 scale、超过 18 位有效数字、非有限值、负 revision、同 `ts_event` 的 revision 未严格递增都会抛 `ColdStoreQualityError`，不落 staging/清单。
- 冷库列补齐 `revision`；`RawBar` 增加 `revision: int = 0`（frozen dataclass 默认值，兼容现有构造点），`bar_record`/`bar_from_record` 同步，热端 `HotStreamBuffer` 用 feed 事件的 revision。同一 `ts_event` 的不同修订可以各占一行。
- 读取语义收敛为公开函数 `latest_visible_bars(bars, *, as_of=None)`：先按 `ts_available <= as_of` 截断，再按 `ts_event` 取最大 `revision`，同 revision 按 `(ts_available, ts_ingested)` 取更晚者。`ColdStore.read_bars(..., as_of=...)` 和快照合并 `_merge_bars` 都调用它；省略 `as_of` 取最新。
- 发布游标：`revision>0` 的修订批次可以回写已覆盖的事件时间；`revision=0` 且不晚于游标的柱仍拒绝，保持普通发布 append-only。游标最后事件时间只增不减。
- 新增一次性迁移命令 `talaria cold-migrate --data-dir ... --pack ...`（实现于 `core/src/talaria/perception/cold_migrate.py`）：逐条读旧字符串 generation → 校验旧文件哈希 → 写新格式文件并读回逐值校验 → 单个 `BEGIN IMMEDIATE` 事务删除旧清单行、插入新行、改指向旧 generation 的游标；旧 Parquet 文件原地保留。重复执行时已迁移的 generation 计为 `already_current`，不切清单；任一步失败回滚清单，旧数据完整。
- 当前 K 线冷库没有费率/资金费率列，因此没有新增费率列；`RATE_COLUMN_SCALE = 8` 仅作为 ADR-0017 的常量记录，衍生品列留给 data-access/08。

**测试与命令（实际运行）**

- `.venv/bin/pytest tests/perception/test_cold_store_decimal128.py tests/perception/test_cold_migrate.py -q` → `15 passed`。
- `.venv/bin/pytest tests/perception tests/foundation tests/workbench_api/test_runtime.py tests/workbench_api/test_app.py tests/evaluation tests/feed -q` → `335 passed, 1 warning`（Starlette/httpx 弃用警告）。
- `.venv/bin/pytest tests/cli -q -k "not real_dump_config_matches_permission_snapshot"` → `51 passed, 1 deselected`。
- `git diff --check`、`compileall` → 通过。
- 覆盖：迁移前后 `Decimal` 逐值相等（含 `809038.73`）；迁移后新旧精度分区混读；超 scale、超 18 位、NaN 拒绝；同一柱两个 revision 在两个 `as_of` 下分别返回；迁移幂等、哈希损坏与超 scale 时清单不变；CLI 迁移后再执行报“无需迁移”；快照修订优先级。
- 未跑全量 `pytest`（按项目规则只跑受影响范围）。

**未验证 / 风险**

- 未在真实数据目录执行迁移；正式迁移前请先停掉 `talaria up` / 其他冷库写入进程并备份 `data/`。迁移的写者保护是进程内的，不能替代停服。
- 迁移在清单切换前会先写新文件；中途失败会留下可复用的孤儿新文件，旧清单和旧文件不受影响，重跑即可。
- `talaria cold-migrate` 在清单不存在或为空时直接返回“无需迁移”，请确认 `--data-dir` 指向正确目录。
- 全量 `tests/cli` 仍有一个与本单无关的既有失败：`tests/cli/test_talaria_desk_profile.py::test_real_dump_config_matches_permission_snapshot`（DSH 预设描述快照不一致），未修改。
- 快照、图表、关键位、`evaluate-levels` 当前并不直接读冷库；它们读 RawStore/热端，因此迁移不改这些路径的数值。冷库读取值由 `read_bars` 迁移前后逐值测试覆盖。
- 写入端只表达 revision（热端透传、冷库列落盘），没有实现 ADR-0018 的持续发布循环；按任务单留给后续切片。

**合并记录（2026-11-22）**

- 并入 `integration/platform-redesign-batch-1`（merge commit `30ab564`）。
- 复跑 `pytest tests/perception tests/foundation tests/workbench_api/test_runtime.py tests/workbench_api/test_app.py tests/evaluation tests/feed -q` → 335 passed。
