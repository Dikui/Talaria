# 05 — 工作台网关按领域拆分路由，组装入口单独成模块

**What to build:** 维护者给某个领域加接口时，只需改该领域的 router 模块。`create_app` 只负责中间件、异常映射、挂载各领域 router 和生命周期。依赖组装（行情源、冷热存储、写队列、各服务、后台 tick 循环）放进一个独立的组装模块，可以单独测试。HTTP/WS 行为、路径、响应模型完全不变。

**Blocked by:** 03 — 协同状态库按领域拆成独立仓储；04 — 启动时按配置选择市场包，场所相关内容全部由市场包提供

**Source of truth:** `docs/architecture.md` §4.1 人机共用分析接口表、§5.1；`contracts/schemas/` 中现有的请求和响应 Schema。

**Status:** resolved

- [ ] 按领域拆分 router：health/feed、instruments/snapshot/indicators/key-levels、drawings/chart-view、watchlist、annotations、alerts、scanner/briefings、collaboration/consultations/usage、WS desk、静态 `/desk` 托管。
- [ ] 组装模块产出一个包含所有服务和后台任务的运行时对象；关闭时按与启动相反的顺序释放资源。加测试：启动后立刻关闭，没有遗留的 task、线程和单写者登记。
- [ ] 新增测试：拆分前后的 OpenAPI 文档（路径、方法、请求和响应模型）完全一致（用拆分前导出的快照对照）。
- [ ] 鉴权中间件现有行为保持不变（写方法需要 Bearer，读方法不需要；本轮不做鉴权加固）。
- [ ] `app.py` 不超过约 200 行；没有任何单个模块超过约 500 行。
- [ ] `tests/workbench_api`、`tests/cli`、`tests/acceptance` 全部通过；`pnpm -C dsh --filter @talaria/desk-ui test` 通过。
