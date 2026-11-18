# 06 — 冷库原子发布、实时切片物化与快照重启可重现

**What to build:** 把 02 的简易落盘升级为架构规范规定的冷热一体时点存储：冷库单写者原子发布，冻结快照时实时热流未归档切片即时物化，进程重启后任意 `snapshot_id` 都能完整重现同一份 `Evidence Snapshot`。

**Blocked by:** 03 — 实时 K 线与盘口流、`NO_TRADES` / `STALE` 断流判定与顶栏健康提示

**Source of truth:** [需求文档](../spec.md)、[领域词汇表](../../../GLOSSARY.md)、[系统分层架构规范](../../../docs/architecture.md)（§2 冷热存储与 SQLite 分库）、[ADR-0006](../../../docs/adr/0006-single-position-round-per-subaccount-and-isolated-ledgers.md)

**Status:** resolved

- [x] 冷库发布遵循“原始报文内容哈希归档 → 临时批次完整性 / 单调性 / 精度质检 → 不可变列式文件原子切换 → 冷库清单库短事务提交游标与版本”；在每一步注入中断，读取侧都看不到半成品批次。
- [x] 冻结快照时，热流中未归入冷库的切片即时物化落盘并登记到实时快照库；列式读取只扫描快照清单显式列出的文件，禁止目录通配符。
- [x] 冷库清单库、实时快照库、协同与感知状态库三个 SQLite 库各自单写者、开启 WAL；退出时刷盘冷库增量并执行 WAL 检查点。
- [x] 每条记录如实写入 `Committed Time`（为 M3 的 `OBSERVED_REPLAY` 预留）。
- [x] 离线测试覆盖中断不暴露半成品、重启后按 `snapshot_id` 逐字节一致重现、清单外文件不被读取。
- [x] 无界面变更，不需浏览器验收。

## Comments

2026-11-19 实现完成，分支 `ticket-06-cold-store`（基线 `66786da`），未合并、未推送。

- 冷库：新增 `core/src/talaria/perception/cold_store.py`。`data/raw_archive/<sha256>.jsonl` 内容寻址归档；`data/staging/<sha256>/bars.parquet` 重建后做完整性/单调性/OHLC/精度质检；通过后 fsync + 原子 rename 到 `data/cold/<venue>/<symbol>/<timeframe>/<sha256>.parquet` 并 fsync 目录；最后在 `data/data_manifest.sqlite` 的 `BEGIN IMMEDIATE` 短事务中写 generations 与 cursors。归档、staging、质检、rename、manifest 前后共 10 个故障注入点；读取只按清单逐个校验 sha256，清单外文件不读。
- 热端：新增 `perception/hot_buffer.py`，`SnapshotBuilder` 增加可选 `hot_bars` provider，同一 `ts_event` 取后摄入版本，`compact_series` 只保留 `ts_available<=cutoff`，快照 Parquet 新增逐柱 `ts_committed`，清单记录整体 `committed_ts`；`read_snapshot_entries` 兼容旧 Parquet。`workbench_api/app.py` 仅在存在实时/回放流时创建 `HotStreamBuffer` 并订阅 `FeedSupervisor` 监听器，退出时先解除监听再停监督器。
- 三库单写者：`foundation/sqlite.py` 提供同进程写者守卫与关闭 checkpoint；冷库、`SnapshotStore`、新增的 `workbench_api/state_store.py`（`StateStore` + 单协程 `StateWriterQueue`，`meta`/`events` 表）均 WAL + `synchronous=FULL`，关闭时 `PRAGMA wal_checkpoint(TRUNCATE)`。
- Committed Time：原始 `RawBar`、冷库归档 JSONL/Parquet、generation 清单、快照 Parquet 与快照清单均保留；`OBSERVED_REPLAY` 仍返回 501。
- 测试：`uv run pytest -q` → 177 passed, 1 deselected；`uv run talaria contract-check` → 零漂移；`pnpm -C dsh typecheck` 与 `pnpm -C dsh build` 通过。新增测试覆盖 28 例冷库（含 10 阶段中断/重试、归档幂等与哈希拒绝、清单外文件、重启字节一致、WAL/单写者）、6 例热端快照、2 例快照清单 WAL、5 例协同状态队列，以及 app 生命周期 checkpoint。
- 冒烟：`uv run talaria up --solo --fixture-dir tests/fixtures/bybit --data-dir <tmp> --port <port>`，经 `/desk` 取 token 创建 `BTC-USDT-PERP` 1m+5m 快照（905/400 根），读回成功；SIGTERM 后两个运行时 WAL 文件消失/清零，同端口重启后按 `snapshot_id` 读回 200 且 Parquet sha256 完全一致，端口释放。
- 已知限制：任务 06 的 ColdStore 是发布/读取库，daemon 尚未接入实时批次发布（当前冷库增量由将来采集发布器调用）；热端缓冲只在存在实时/回放流且对应通道被 WS 订阅时积累，与 03 的按需订阅语义一致。

2026-11-19 lead review 后追加：daemon 级冷库发布已接入（同一分支，后续提交）。

- 新增 `core/src/talaria/perception/cold_publisher.py`：`ColdPublisher` 持有 `ColdStore` + `InstrumentSpec` + timeframe，`publish_snapshot_bars()` 按 `ts_event` 排序、同 timestamp 后出现者胜、丢弃空值柱，再按冷库游标只保留 `ts_event > cursor.last_ts` 的新柱；无新柱返回 `None`。
- `ColdStore.publish()` 在“同内容已发布”判断后新增 append-only 游标守卫：`cursor.last_ts >= 批次最小 ts_event` 时抛 `ColdStoreQualityError`，旧批次/重叠批次绝不发布，游标不回退。
- `SnapshotBuilder` 新增可选 `cold_publishers: Mapping[(instrument_id, timeframe), ColdBarPublisher]`；每个周期在合并 RawStore 与热端后，把 `ts_available<=cutoff` 的柱尽力发布到冷库；`ColdStoreError` 只记 warning，不阻塞快照创建。
- `workbench_api/app.py` lifespan 创建 `ColdStore(data_dir)`，为配置中的全部 instrument/timeframe 构建 publisher，注入 `SnapshotBuilder`；退出时在 supervisor、`SnapshotStore`、`StateWriterQueue` 关闭之后关闭冷库并执行 `wal_checkpoint(TRUNCATE)`。
- 验证：`uv run pytest -q` → 187 passed, 1 deselected；`uv run talaria contract-check` → 契约生成物零漂移。新增测试覆盖游标/排序/去重/空柱、旧批次与重叠批次拒绝、builder 可见柱过滤与发布失败降级、fixture daemon 快照生成冷库 generation、二次快照不重复、重启后按 manifest 读回冷柱、退出后无 `*-wal`。CLI 冒烟（fixture + `--solo`）确认 `data_manifest.sqlite` 与 1 个 BTC 1m generation、900 根真实柱；SIGTERM 后 WAL 为 0，重启后 `snapshot_id` 仍 200。
- 上面“daemon 尚未接入实时批次发布”的已知限制已被本次提交取代；冷库仍以快照构建为发布触发点，不做独立后台采集循环。


## Lead 验收

2026-11-19 合并 `v1-analysis-workbench` 任务单 06，合并提交 `188b353 Merge branch 'ticket-06-cold-store'`（分支末提交 `3309cef`）。

- 合并 `workbench_api/app.py` 时与 04 的指标路由/`live=1` 接线冲突；手工保留两者：04 的 `GET /api/snapshots/{id}/indicators`、`POST /api/operators/compute` 与 06 的 `ColdStore`/`ColdPublisher`/`HotStreamBuffer`/`StateWriterQueue` 生命周期共存。
- 合并后主分支验证：`uv run pytest -q` → 237 passed, 1 deselected（04+06 合计，仅 Starlette/httpx 弃用 warning）；`uv run talaria contract-check` → 零漂移；`pnpm -C dsh typecheck` 通过；`pnpm -C dsh --filter @talaria/desk-ui test` → 42 passed；`pnpm -C dsh build` 通过（JS 490.62 kB / CSS 13.55 kB）。
- 冷库现在真正接入 daemon：快照构建时按周期发布可见柱到 `data/cold/...` 与 `data/data_manifest.sqlite`，append-only 游标拒绝旧/重叠批次，快照失败不阻塞；退出顺序 supervisor→SnapshotStore→StateWriterQueue→ColdStore，`wal_checkpoint(TRUNCATE)`。
- 接受已知限制：冷库发布仍以快照构建为触发点，无独立后台采集循环；已发布 timestamp 的迟到 revision 不回溯（append-only 游标）；“原始报文”按标准化 `bar_record` 归档，非 Bybit HTTP 原始响应。
