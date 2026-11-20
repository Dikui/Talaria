# 02 — 协同状态库的写入移到专用线程，读取不再排在写队列后

**What to build:** 交易员在看盘页拖动画线、确认预警、收会诊结果时，实时 K 线推送和预警 tick 不再被 SQLite 的 `fsync` 卡住。`StateWriterQueue` 保留“唯一写者、按提交顺序串行、失败对调用者可见”的语义，但真正的 SQLite 调用放到一条专用线程里执行，事件循环只等待 future。纯读取走独立的只读连接（WAL 下可以和写者并发），不再排在写队列后面。

**Blocked by:** 01 — 把有状态引擎和 I/O 模块移出 L0 foundation

**Source of truth:** `docs/architecture.md` §5.2（`collaboration_state.sqlite` 由 `StateWriterQueue` 单写者串行写入）；ADR-0005（快通道零阻塞）；ADR-0006（按写者分库）。

**Status:** resolved

- [ ] 所有写操作仍由同一个写连接按入队顺序串行执行；写连接只在专用线程上使用。
- [ ] 读取接口使用 `query_only` 的只读连接，不经过写队列；能读到调用前已经提交的写入（read-your-writes）。
- [ ] 新增测试：写操作执行期间（用可控阻塞模拟慢 fsync），事件循环上的另一个协程能按时推进，写操作不会阻塞事件循环。
- [ ] 新增测试：并发提交 N 个写操作，落库顺序等于提交顺序；某个操作抛异常时只有它的调用者收到异常，后续操作照常执行。
- [ ] 关闭流程：排空队列、截断 WAL、释放单写者登记、结束线程；重复关闭幂等；关闭后再提交会抛明确的错误。
- [ ] 必须保留：同一进程内对同一库的第二个写者打开时立即失败（单写者守卫）。
- [ ] `tests/workbench_api` 与 `tests/acceptance` 全部通过。
