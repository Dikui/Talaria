# DeepSeek Harness (`dsh`) 运行底座、插件机制与量化生态核查

- **调研日期**：2026-11-18（Asia/Shanghai）。
- **范围**：`deepseek-ai/deepseek-harness` 官方架构、运行模板、插件 API、Python SDK、已公开安全边界，以及围绕 `dsh` 构建的社区交易/量化扩展包。
- **方法**：区分官方仓库与文档事实、社区插件现状与面向 Talaria 的部署设计建议；未在本轮全局安装或启动外部交易账户连接器。

---

## 1. 官方仓库与版本状态事实

- **项目定位**：DeepSeek AI 于 2026-08-13 开源的 MIT 许可智能体运行底座（仓库 `deepseek-ai/deepseek-harness`，命令行入口 `dsh`，官方文档站 `https://deepseek-harness.github.io/deepseek-harness/`）。
- **核心架构（Cordis 微内核）**：底层基于 `cordisjs` / `@deepseek-ai/cordis` 时空可组合依赖注入内核实现 **"Everything is a Plugin"**——模型适配器、工具注册表、会话持久化、沙箱、前端插槽乃至主智能体循环本身均为可替换插件，无需 fork 核心仓库即可拼装领域系统。
- **版本稳定性边界**：截至 2026 年 11 月处于 **`v0.2.x` Developer Preview** 阶段（如 `0.2.1` / `0.2.2-rc.1`），官方 README 明确提示预期存在破坏性兼容变更（compatibility-breaking changes）。**推论**：核心金融数据、定仓风控、模拟撮合与账本不应直接耦合在 DSH 插件内部，应保持薄插件桥接层并精确锁定 `@deepseek-ai/dsh*` 与 `deepseek-harness-sdk` 版本号。

---

## 2. 五大官方 Profile 模板与双向通信机制

根据官方 `docs/architecture.md` 与 `packages/README.md`，`dsh` 通过堆叠 Bundle 构建隔离 Profile（默认位于 `$DSH_HOME/profiles/<name>`）：

| 官方模板 | 组装结构 | 通信与运行特征 | Talaria 采用策略 |
|---|---|---|---|
| **`web`** | `dsh-base` + `@deepseek-ai/dsh-web-app` | 默认在 `127.0.0.1:3080` 启动浏览器工作台与 BFF | 派生 `talaria-desk`，用于实时影子模拟（`Shadow Run`）看盘与轨迹溯源 |
| **`headless`** | `dsh-base` + `dsh-headless` | 不启动 Web 服务的单次任务运行器 | 用于盘前/盘后例行健康巡检与批处理归因 |
| **`sdk`** | `dsh-base` + `@deepseek-ai/dsh-sdk-app` | 暴露 `dsh-sdk-jsonrpc-server`（`stdio` 换行分隔 JSON-RPC 2.0） | 供 Python 守护进程长连接驱动完整能力会话 |
| **`sdk-minimal`** | 独立精简树（不继承 `dsh-base` 全套默认工具） | 仅加载显式声明的最小工具集，官方亦用于基准评测 | **派生 `talaria-eval`**，供历史回测纯净调用，物理剔除 `shell`、外网与文件写工具 |
| **`acp`** | `dsh-base` + `dsh-acp-app` | Agent Client Protocol 自动化服务接口 | 首版无需启用 |

### 关键 API 与扩展原语核查
1. **插件声明与覆盖（`dsh.bundle` + `cordis.patch.yml`）**：第三方包在 `package.json` 声明 `"dsh": { "bundle": { "patch": "./cordis.patch.yml" }, "client": { "platform": "web" } }`。**重要细节**：`cordis.patch.yml` 与 `--patch` 按唯一 `id` 执行**整行配置替换而非深合并（Deep Merge）**，因此必须配合 `dsh --dump-config` 做完整配置快照测试。
2. **工具定义与双态输出（`@deepseek-ai/dsh-tools`）**：`defineTool` 将 `execute(args)` 返回的结构化 JSON（供编程式工具调用 PTC 直接读取）与 `output.schema` + `output.render`（生成 UI `ContentBlock`）解耦。
3. **单调执行守卫（Monotonic Execution Guards）**：在 `tools/pre-execute` 之后、`tools/execute` 之前运行；守卫返回原因字符串即拒绝执行，返回 `undefined` 则不阻断；**不存在 `'allow'` 返回值，下游插件无法推翻上游守卫的拒绝裁决**。
4. **闭合单次审批链（`@deepseek-ai/dsh-user-approval`）**：`ApprovalOutcome` 严格定义为 `'allowed-once' | 'rejected' | 'cancelled' | 'unavailable'`，带品牌化 `ApprovalRequestId` 审计链；回答器缺失或抛错时落入 `'unavailable'` 并执行 **fail-closed**。
5. **PTC 运行时（`@deepseek-ai/dsh-ptc-runtime`）**：自 `0.1.0-rc.7` 起由 Code Mode 更名为 PTC Mode，允许模型编写受控代码连续调用工具做跨序列计算，避免心算误差。
6. **强类型前后端 RPC 与 UI 插槽**：`@deepseek-ai/dsh-typert-protocol`（`@Remote` + 共享 Zod Schema）负责宿主与浏览器强类型调用；`@deepseek-ai/dsh-client-ui-slots`（`SlotMap` 声明合并、`single/list/keyed/chain` 槽位与 `dsh-resource://`）负责无侵入挂载前端卡片。
7. **跨平台沙箱（`@deepseek-ai/dsh-sandbox`）**：支持 **macOS `Seatbelt`**、Linux `bwrap` / `Landlock` 与 SSH 远端隔离。
8. **Python 官方驱动（`deepseek-harness-sdk`）**：PyPI 官方子进程封装，强制显式传入 `dsh_home` 绝对路径，通过 `stdio` JSON-RPC 2.0 启停会话并读取 `RunResult`。

---

## 3. 必须规避的安全与集成陷阱

1. **沙箱逃逸历史漏洞警示（`CVE-2026-82533`）**：早期版本因沙箱内进程可无鉴权请求宿主本地端点切换 `danger-full-access` 导致沙箱与审批被绕过。部署时必须锁定已修复版本，研究沙箱禁连宿主控制端口，且 Python 底座绝不提供运行时热改交易模式或风控上限的接口。
2. **会话内 `/permission` 命令与 `fork` 轨迹重放**：不能仅靠 `DSH_PERMISSION_MODE` 环境变量限制工具权限——`talaria-eval` 回测 Profile 必须在 `cordis.patch.yml` 层物理不挂载无关工具；同时通过单调执行守卫拦截从旧轨迹 `fork` 分支出的重放提交。
3. **MCP 工具输出 Schema 严格子集校验**：`@deepseek-ai/dsh-mcp-client` 的 `assertSupportedJsonSchema()` 仅接受 `type / oneOf / properties / required / additionalProperties / items / enum / const` 等受限关键字，包含根级 `$schema` 或未展开 `$defs` 会抛出 `JsonSchemaError`。

---

## 4. 社区金融与量化插件生态对标

| 社区项目 | 已核实特点 | 对 Talaria 的启示与边界 |
|---|---|---|
| **`dsh-trading`** (`zhu1090093659/dsh-trading`) | 零侵入插件包拼装三栏多市场终端；19+ 连接器；`tools/pre-execute` 审批瀑布流；维护 `dsh-seam-inventory.md` 缝隙核查清单。 | 借鉴其插件分包、UI 插槽组织与缝隙清单方法；但其浏览器端单标的纯函数回测不能替代双时间 PIT 快照与高保真延迟模拟器。 |
| **`dsh-quant` / `dsh-quant-indicators`** | 核心指标计算保持零 `dsh` 导入，外包薄 `defineTool`；采用前导 `null` 等长对齐数组与 `bar[i]` 确认、`bar[i+1]` 成交约定。 | 直接采纳“前导 `null` 等长序列”与“零框架耦合计算核”规范，提升 PTC 模式多序列下标对齐可靠性。 |
| **`quantitativetrading-source-reliable`** | 收录 119 个量化与加密数据源的 PIT 标志、修订风险与引用审计工具。 | 印证了在认知层前置数据源可用时间与引用合规核查的必要性。 |

---

## 5. 参考入口

- 官方仓库与文档：`https://github.com/deepseek-ai/deepseek-harness`、`https://deepseek-harness.github.io/deepseek-harness/`
- Python SDK：`https://pypi.org/project/deepseek-harness-sdk/`
- 社区插件索引与样本：`https://dsh-plugin.org/`、`https://github.com/zhu1090093659/dsh-trading`
