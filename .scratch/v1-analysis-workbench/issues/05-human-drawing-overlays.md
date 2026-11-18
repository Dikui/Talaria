# 05 — 人工画线（`HUMAN_DRAWING`）持久化、撤销重做与视窗回读

**What to build:** 交易员在看盘页的 `HUMAN_DRAWING` 图层绘制和拖拽编辑结构线，松手后自动保存，刷新或切换周期后按可见范围恢复，可撤销重做；共享视窗回读接口 `get_chart_view` 返回当前视窗与其中的人工画线，供预警与智能体会诊读取。

**Blocked by:** 02 — 最窄贯通：Bybit 历史 K 线回填、证据快照冻结与 `--solo` 看盘页显示 K 线

**Source of truth:** [需求文档](../spec.md)、[领域词汇表](../../../GLOSSARY.md)、[系统分层架构规范](../../../docs/architecture.md)、[ADR-0005](../../../docs/adr/0005-fast-path-bypass-and-decoupled-chart-workbench.md)

**Status:** ready-for-agent

- [x] 轻量 React 画线工具栏支持水平线、射线、线段、平行通道、斐波那契回撤、矩形区间与文本标签。
- [x] 拖拽过程在前端乐观重绘，不等待后端；松手后经快通道写入协同与感知状态库（进程内单一异步串行写队列独占写入，SQLite WAL）。记录含十进制字符串价格坐标、`Instrument`、单周期 / 跨周期可见范围、递增版本号与软删除时间戳。
- [x] 基于版本链的撤销、重做、单条删除与按当前 `Instrument` 清理；刷新或切回周期后准确恢复。
- [x] `get_chart_view` 返回当前 `Instrument`、周期、可视时间-价格窗口及窗口内全部有效 `HUMAN_DRAWING`（本接口只在此任务实现）。
- [x] 浏览器界面验收（均附截图）：宽度 `1440` 下浅色与深色、简体中文检查画线工具栏、锚点选中态与属性浮层，覆盖长文本标注在图表边缘不被裁切、当前 `Instrument` 无画线时的管理列表空态；宽度 `390` 下检查已有画线只读显示、工具栏隐藏。

## Comments

- 2026-11-19 实现完成，分支 `ticket-05-drawings`（未合并、未推送）。
- 后端：`core/src/talaria/foundation/drawings.py` 定义 `DrawingKind`/`DrawingPoint`（价格 `DecimalStr`）/`DrawingCreate`/`DrawingPatch`/`DrawingRecord`/`ChartView` 与时间-价格可见性规则；`workbench_api/state_store.py` 新增 `drawings` 与 `drawing_versions` 表、WAL + `synchronous=FULL`，全部写操作经 `StateWriterQueue.run()` 单协程串行执行；`workbench_api/drawing_service.py` 负责参数校验与业务编排，未知 `drawing_id` → `DRAWING_NOT_FOUND` 404。
- API：`GET/POST/DELETE /api/drawings`、`GET /api/drawings/{id}`（`?version=` 读历史版本）、`GET /api/drawings/{id}/versions`、`PATCH /api/drawings/{id}`、`DELETE /api/drawings/{id}`（软删除）、`POST /api/drawings/{id}/restore`、`GET /api/chart-view`（即 `get_chart_view`）。写操作 Bearer token 鉴权，缺失/错误 401；参数错误 422。契约门禁已登记 10 个新 schema，`uv run talaria contract-check` 零漂移。
- 前端：`features/drawings/` 下新增几何、视窗、版本链、串行写队列、图表投影与 `useDrawings` 控制器，以及工具栏/覆盖层/属性浮层/管理列表组件；`MarketChart` 仅增加 `overlay`/`onChartReady` 两个接线点，`App` 仅负责组装。SVG 覆盖层用 `convertToPixel`/`convertFromPixel`（`absolute:true`）按 KLineChart 根坐标对齐，价格只在像素转换最后一跳转 `number`；长文本在图表右缘向左翻转并按可用宽度截断，不会被画布裁切；宽度 ≤400px 时工具栏与管理/属性控件不渲染，已有画线仍只读显示。
- 测试：`uv run pytest -q` → 256 passed, 1 deselected；`uv run talaria contract-check` → 契约生成物零漂移；`pnpm -C packages/talaria-desk-ui typecheck`、`test`（13 文件 / 75 tests）、`build` 全绿。
- 浏览器验收（ego-browser，`uv run talaria up --solo --fixture-dir tests/fixtures/bybit --data-dir /tmp/talaria-ticket05/data --port 8735`，页面 `http://localhost:8735/desk`）：
  - 1440×900 浅色工具栏：断言工具栏 7 个画线工具与撤销/重做/管理/清空标的可见；截图 `/Users/dikui/Project/Talaria/.agents/worktrees/ticket-05-drawings/.scratch/tasks/ticket-05/01-light-toolbar.png`。
  - 1440×900 浅色锚点选中 + 属性浮层：创建水平线后锚点圆点可见、右侧属性浮层显示「水平线 v1」；截图 `.../02-light-selected-inspector.png`。
  - 1440×900 长文本贴边：55 字标注在图表右缘向左翻转，文本 bbox 右界 1373 < 覆盖层宽 1402，未裁切；截图 `.../03-long-text-edge.png`。
  - 1440×900 深色：工具栏与两条画线可读；截图 `.../04-dark.png`。
  - 空管理列表：无画线时打开「管理」显示「当前窗口暂无画线，使用上方工具栏开始绘制。」；截图 `.../05-empty-manager.png`。
  - 390×844 只读：`matchMedia('(max-width: 400px)')` 命中后工具栏/属性浮层/管理列表均不渲染、2 条画线仍显示；截图 `.../06-390-readonly.png`。
  - 交互断言：刷新页面后画线按窗口恢复；切到 5m 时仅 1m 可见画线消失、切回 1m 恢复；撤销 → 后端版本链出现 `deleted_at`（v2 软删），重做 → 恢复为 v3；拖拽水平线 → PATCH 生成 v2 且价格坐标变化；文本修改 → v3。写操作均经单一串行队列落库。
- 已知限制：撤销/重做栈保存在当前页面会话内，不跨刷新持久化；「清空标的」后的撤销只能恢复当前窗口已加载的画线；射线/线段等端点拖拽只移动锚点，不提供独立控制点手柄；宽度 390 的验收通过 CDP `Emulation.setDeviceMetricsOverride` 取得精确视口（ego 浏览器窗口本身为 1028×848）。验收期间同机存在其他任务的 Talaria 实例（127.0.0.1 不同端口共享 cookie 域）导致 token 覆写，故改用 `localhost` 域名隔离 cookie，不属于产品缺陷。
