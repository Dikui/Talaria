# 03 — 协同状态库按领域拆成独立仓储

**What to build:** 维护者修改画线、标注、自选、关键位反馈、预警、会诊、简报、用量中任意一个领域的持久化时，只需打开该领域自己的仓储模块。每个仓储拥有自己的表结构、建表语句和读写方法，共用 02 提供的同一个写连接和写线程。服务层通过 `store.<domain>.<method>` 访问。现有库文件不需要迁移，表名和列名都不变。

**Blocked by:** 02 — 协同状态库的写入移到专用线程，读取不再排在写队列后

**Source of truth:** `docs/architecture.md` §5.2；`GLOSSARY.md` 中各领域术语（画线、AGENT_ANNOTATION、Watchlist、预警、会诊、简报）。

**Status:** ready-for-agent

- [ ] 按领域拆分为 meta/events、drawings、annotations、watchlist、key_level_feedback、alerts、consultations、briefings、usage 九个仓储；每个仓储独立成模块，单个模块不超过约 400 行。
- [ ] 表名、列名、索引和外键都不变：拿旧版本创建的 `collaboration_state.sqlite` 启动新代码，所有数据都能读出（加一条用旧建表语句生成库再读回的测试）。
- [ ] 各服务（画线、标注、预警、会诊、简报、自选、关键位反馈）只通过自己领域的仓储访问数据，不跨域调用别的仓储的私有方法。
- [ ] 搜索无残留：`StateStore` 上不再有领域方法（只剩连接生命周期和仓储组合）；`state_store.py` 不超过约 300 行。
- [ ] `tests/workbench_api/test_state_store.py` 按领域拆开，并随仓储移到对应位置；`tests/workbench_api`、`tests/acceptance` 全部通过。
