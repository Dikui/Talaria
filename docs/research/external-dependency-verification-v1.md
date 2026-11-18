# 外部依赖版本与许可证核实（v1）

- **调研日期**：2026-11-19（Asia/Shanghai）。
- **工作分支**：`ticket-01-deps`，基线 `efc2a75230dc99cfafb4e6a5575e930cac1ed186`。
- **范围**：DeepSeek Harness（`dsh` / Python SDK / ui-slots / typert）、KLineChart v10、TA-Lib 与 talipp、四个关键位参考项目的许可证。
- **方法**：只使用一手来源——官方仓库与 releases、npm/PyPI 元数据与 tarball/wheel、仓库内 `LICENSE`、官方文档；本机可核验安装 `dsh 0.2.2-rc.3` 与 `/Applications/DeepSeek Harness.app` 也作为一手证据。临时探针全部在 `/tmp`，未向仓库安装任何业务依赖。
- **证据边界**：版本号与许可证结论截至上述日期；DSH 处于 0.x 预览，升级后必须重跑本文探针。

---

## 0. 结论速览

| 依赖 | 锁定值 / 结论 | 阻塞结论 |
|---|---|---|
| DeepSeek Harness 运行时 | **无 stable release**；最新 `0.2.2-rc.5`（prerelease），本地基线 **`0.2.2-rc.3`** | 不阻塞 09，但须按 §1.6 约束 |
| `deepseek-harness-sdk` | **`0.2.2rc2`**（PyPI 唯一最新；自带 runtime-bin `0.2.2rc2`） | 可与本地 rc.3 握手；完整 turn 未验 |
| ui-slots / typert | 仍 **0.x 预览**；typert `0.2.2-rc.3`（本地）/ `0.2.2-rc.5`（npm next）；ui-slots npm `latest=0.0.1-rc.1`、`next=0.2.2-rc.5` | 不阻塞，但锁精确版本 |
| KLineChart | **`10.1.1` 正式版**（2026-11-16，非 beta/rc），**Apache-2.0** | 不阻塞 02 |
| `registerIndicator` 外部结果 | 不能直接注入 `result`；用注册模板 + `calc` 返回外部数组 | 可实现，见 §2.2 |
| `setDataLoader` | v10 分页接口可用：`init/forward/backward/update` + `callback(data, more)` | 不阻塞 02 |
| TA-Lib C / Python 封装 | C **`0.8.3` BSD-3-Clause**；Python **`0.8.3` BSD-2-Clause** | 不阻塞 04 |
| TA-Lib wheel | `cp314` + `macosx_14_0_arm64` 存在，Python 3.14.6 实测可用 | 无需回退 |
| talipp | **`2.7.0` MIT**（2025-09-09；维护低频） | 不阻塞 04 |
| 参考项目许可证 | SMC / trendln / market-structure 均 MIT；support_resistance **无 LICENSE 文件**，仅 `setup.py` 写 `wtfpl` | 不阻塞 10；后者标记不可复制 |

---

## 1. DeepSeek Harness

### 1.1 当前稳定版本：不存在 stable

- GitHub Releases 共 41 个 release，**非 prerelease 数量为 0**；最新为 `dsh-v0.2.2-rc.5`（2026-11-18T12:51:39Z），全部为 `prerelease: true`。仓库 tags 中也没有去掉 `rc/alpha/beta` 的稳定 tag。
  - <https://github.com/deepseek-ai/deepseek-harness/releases>
- npm `@deepseek-ai/dsh` 的 dist-tags 并不指向最新：`latest=0.2.2-rc.1`、`next=0.2.2-rc.5`、`alpha=0.2.2-alpha.3`；许可证 MIT。**不能按 `latest` 标签安装**。
  - <https://www.npmjs.com/package/@deepseek-ai/dsh?activeTab=versions>
- 本机可核验安装为 **`dsh 0.2.2-rc.3`**（`/opt/homebrew/bin/dsh` → `/Applications/DeepSeek Harness.app/Contents/Resources/runtime/cli/bin/dsh`）。

**结论**：不存在“当前稳定版本”。建议团队基线锁定 **`0.2.2-rc.3`**（本报告所有 dump/guard/SDK 握手均在此版本验证）；`0.2.2-rc.5` 是更新候选，但未在本轮重跑探针。任何升级都必须重跑 §7 的命令并复核 §6 的回改位置。

### 1.2 SDK、ui-slots、typert 仍是 0.x 预览

**Python SDK**：PyPI `deepseek-harness-sdk` 最新为 `0.2.2rc2`（2026-11-11T12:56:30），只有 `py3-none-any` wheel；同版本依赖 `deepseek-harness-runtime-bin==0.2.2rc2`。没有 rc.3/rc.5 的 Python SDK 发布。
- <https://pypi.org/project/deepseek-harness-sdk/#history>
- 版本号仍是 `0.2.2rc2`，属于 0.x 预发布；不能视为稳定 API。

**npm 侧**（本地 rc.3 实际安装 / npm 最新）：

| 包 | 本地 rc.3 | npm 当前 | 许可证 | 结论 |
|---|---|---|---|---|
| `@deepseek-ai/dsh-sdk-protocol` | `0.2.2-rc.3` | `0.2.2-rc.5`（next） | MIT | 0.x |
| `@deepseek-ai/dsh-sdk-jsonrpc-server` | `0.2.2-rc.3` | `0.2.2-rc.5` | MIT | 0.x |
| `@deepseek-ai/dsh-typert-protocol` | `0.2.2-rc.3` | `0.2.2-rc.5` | MIT | 0.x |
| `@deepseek-ai/dsh-typert-registry` | `0.2.2-rc.3` | `0.2.2-rc.5` | MIT | 0.x |
| `@deepseek-ai/dsh-client-ui-slots` | 桌面安装未作为独立包出现（npm 单独发布） | `latest=0.0.1-rc.1`，`next=0.2.2-rc.5` | 旧 `latest` 为 BSD-3-Clause；`next` 为 MIT | 0.x；升级须重核 LICENSE |

- `ui-slots` 的 npm `latest` 仍停在 `0.0.1-rc.1`，与当前 0.2.2 线不是同一版本；不要用 `latest`。
- typert/ui-slots 包版本均为 `0.x.y-rc.*`，没有 1.0 稳定线。

**近期破坏性变更**：`docs/upgrade-guide/` 下 `0.2.1-rc.1..rc.4` 与 `0.2.2-rc.1..rc.4` 的逐项说明中，**没有** sdk / typert / ui-slots 的专项破坏性变更条目；只有 rc.1 的 `ptc-bootstrap-protocol` 等工具/运行时变更。rc.3 另有 `working-directory-tool-removed`、`cordis-loader-entry-validation` 等，但不属于这三者。结论是“0.2.2 周期未记录专项 break”，而非“接口稳定”；0.x 预览不提供 semver 保证。
- <https://github.com/deepseek-ai/deepseek-harness/tree/master/docs/upgrade-guide>

**SDK 与本地 rc.3 的兼容性**：Python `0.2.2rc2` 的 `HarnessConfig` 提供 `dsh_bin: str | None = None`、`profile: str = "sdk"`、`patches`、`dsh_home`、`cwd`、`env`；`dsh_bin` 为空时才使用自带的 `deepseek-harness-runtime-bin`，且每次启动都要求显式 `dsh_home`/`DSH_HOME`（不会自动发现 `~/.dsh`）。
- 实测：`dsh_bin=/opt/homebrew/bin/dsh`、`dsh_home=/tmp/dshhome-probe`、`cwd=/tmp`，SDK `0.2.2rc2` 对本地 `dsh 0.2.2-rc.3` 完成 `initialize` 与 `shutdown`，输出 `INIT_OK True`、`CLOSED_OK`。
- 限制：只验证了握手，没有发起真实模型 turn；`session/prompt` 的完整事件流与工具调用未验证。

### 1.3 “每轮结束”宿主钩子：有事件，但没有现成 usage 字段

**结论**：有持久化的 `turn/end` 事件，可作为每轮结束触发点；但 **`turn/end` 的 payload 只有 `{ turn, reason }`，不含 token 用量**。用量在 `assistant/message` 的流式 usage 与 `llm/retry` 等 attempt 事件里，必须按 turn 折叠。

证据：

- `docs/agent-lifecycle.md`：durable replay 事实在 `session/event`；SDK 用户应消费 `session/event`；`turn/end` 是持久事件；`agent/turn-stopping` 是 natural stop 的 serial terminal checkpoint，且只被少数插件监听，不是通用 turn-end 钩子。
  - <https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/agent-lifecycle.md>
- SDK 协议 `SessionEventNotification`（`packages/sdk/protocol/src/types.ts:153-159`）：
  ```ts
  interface SessionEventNotification {
    sessionId: string
    event: SessionEvent // full session-log envelope
  }
  ```
  即 SDK 的 `session.event` 通知携带完整事件信封，可过滤 `event.type === 'turn/end'`。
  - <https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/sdk/protocol/src/types.ts#L153-L159>
- `turn/end` 事件定义（`packages/core/session/src/types.ts:307`）：
  ```ts
  'turn/end': { turn: number; reason: TurnEndReason }
  ```
  - <https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/core/session/src/types.ts#L307>
- `deriveTurnTokenUsage`（`packages/llm/token-meter/src/turn-usage.ts`）的精确签名与语义：
  ```ts
  export function deriveTurnTokenUsage(
    events: readonly SessionEvent[],
  ): TurnTokenUsage | undefined
  ```
  `TurnTokenUsage` 字段：`uncachedInputTokens`、`outputTokens`、`totalTokens`、可选 `cacheReadTokens` / `cacheWriteTokens` / `reasoningTokens` / `routes`；要求从 `turn/start` 到 `turn/end` 的完整 attempt 生命周期，缺边界、计数不安全或精确总数冲突时返回 `undefined`。
  - <https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/llm/token-meter/src/turn-usage.ts>
- **该函数不是公开 API**：`packages/llm/token-meter/src/index.ts` 只有 `export type * from './types.ts'`，没有导出 `deriveTurnTokenUsage`；package 的 `exports` 只有 `.`、`./client`、`./estimate`、`./src/*` 等，没有 `./turn-usage` 稳定入口。
  - <https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/llm/token-meter/src/index.ts#L43>
  - 本地 `lib/index.js` 实测也不包含 `deriveTurnTokenUsage` / `turn-usage` 字符串。

**账本字段映射**（实现时不要重复计数）：

| 账本字段 | 来源 |
|---|---|
| 输入（未缓存） | `uncachedInputTokens`（各 attempt 的 `inputTokens` 求和） |
| 缓存命中 | `cacheReadTokens` |
| 缓存写入 | `cacheWriteTokens`（可选；没有该桶时不要补 0） |
| 输出 | `outputTokens` |
| 精确 prompt 总量 | `totalTokens - outputTokens`（需要时计算） |
| 精确总计 | `totalTokens` |

**实现硬约束**：

1. `dsh-bridge` 订阅 `session/event`（SDK 宿主用 `session.event` 通知），从 `turn/start` 起缓存该 turn 的事件；收到 `turn/end` 后折叠全部 attempt 的 usage。
2. 不要从 `turn/end` 直接取 usage；也不要把 `agent/turn-stopping` 当作通用钩子（它只在 natural stop 的串行终态检查点触发）。
3. 折叠逻辑没有公开 helper：TypeScript 侧需自行实现（或固定版本下深引内部模块，但不建议）；Python 侧（`talaria-auto`/`talaria-eval`）需实现同构折叠，并用同一份捕获事件流做黄金测试，防止两端口径漂移。
4. **幂等键**：协议中的 `turn` 是会话内序号，不是全局 UUID；账本幂等键应为 `(dsh_session_id, turn)`（或 bridge 生成稳定的复合 ID），不能只用 `turn`。
5. 折叠返回 `undefined` 时不得按 0 入账；应标记待补账/`UNALLOCATED`，再按架构 §3.6 的“扫描 `$DSH_HOME` 轨迹补齐漏报”路径对账。
6. `turn/end` 的 `reason` 标识本轮结束原因；异常终态的 usage 可能不完整，需按“可证明才入账”处理。

### 1.4 单调执行守卫：存在且单调，但 dump-config 看不到

- API 存在：`ctx.tools.guard(guard)`，在可扩展的 `tools/pre-execute` 之后、`tools/execute` 之前运行；守卫返回原因字符串即拒绝，返回 `undefined` 才放行；**没有 `'allow'` 返回值，后续监听器不能把拒绝翻回允许**。
- 源码签名（`packages/core/tools/src/contracts.ts:403-410`）：
  ```ts
  export type ToolGuard = (
    execution: Readonly<ToolExecution>,
  ) => string | ToolGuardDenial | undefined
  ```
  `src/index.ts:384` 为 `guard()` 注册；`src/pipeline.ts:214` 在 `decision.kind === 'allow'` 后调用 `checkGuards(exec)`，`checkGuards` 返回第一个 denial。
  - <https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/core/tools/src/contracts.ts#L403-L410>
  - <https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/core/tools/src/pipeline.ts#L214>
- 没有名为 `monotonic-guard` 的独立包；它是 `@deepseek-ai/dsh-tools` 的注册 API。
- **关键事实**：`dsh --dump-config --profile web` 的输出中 `grep -ci guard` 为 **0**。守卫是运行时由插件注册的函数，不是 composed config 的字段；dump-config 能快照 permission/approval 配置，但**不能证明守卫已注册或其行为**。

### 1.5 `dsh --dump-config` 现状与输出格式

在本机 `dsh 0.2.2-rc.3`、`DSH_HOME=/tmp/dshhome-probe`（干净用户层）实测：

| 命令 | 退出码 | 结果 |
|---|---|---|
| `dsh --dump-config`（无 `--profile`） | 1 | stderr `error: --profile <name> is required`；无输出 |
| `dsh --dump-default-config`（无 `--profile`） | 1 | 同上 |
| `dsh --dump-config --profile web` | 0 | **46,413 bytes**，**202** 个 `- id:`，**46** 处 `!!js`，105 处 `config:`；首行 `# == @deepseek-ai/dsh-base` |
| `dsh --dump-config --profile sdk` | 0 | **13,887 bytes**；首行 `# == @deepseek-ai/dsh-base, patched by @deepseek-ai/dsh-sdk-app` |
| `dsh --dump-default-config --profile web` | 0 | 干净 home 下与 dump-config 同尺寸；用于排除用户层 |
| `dsh --dump-config-schema --profile web` | **1** | stdout 为 **946,214 bytes 的合法 JSON Schema**；stderr 有 4 条 `error: [/198..201] unrecognized Loader tree carrier; use cordis:group or cordis:include for native child collection`，另有 `/120,/128,/129,/162` warnings |

输出格式：YAML 风格的 composed profile tree（注释头 + `- id` / `name` / `disabled` / `config`），含 `!!js` 自定义 tag。**不是可直接 `JSON.parse` 的格式；标准安全 YAML loader 也会因未知 tag 失败**。建议按文本逐行快照，或使用能处理自定义 tag 的 loader。
- `--dump-config` 的帮助文案："print the composed profile tree and exit"；`--dump-config-schema`："print JSON Schema ... without mounting"；`--dump-default-config`："print profile tree without user layer"。
- `--dump-config` 的 profile 中确有 `- id: approval`（`@deepseek-ai/dsh-user-approval`）与 `- id: permission`（`@deepseek-ai/dsh-permission-presets`，read-only / workspace-write / danger-full-access），但**没有 guard 记录**。

### 1.6 对任务 09 的阻塞结论

**不阻塞设计/开发启动**，但 09 的实现必须接受以下事实：

- 无 stable DSH；只能锁 `0.2.2-rc.3` + SDK `0.2.2rc2`，并把“升级即重验”写进维护流程。
- 每轮 usage 只能靠事件折叠；没有公开 helper；TS 与 Python 两端需同构实现并做黄金测试。
- 单调守卫不能用 dump-config 快照证明；必须增加“挂载 profile + 触发被禁工具”的行为测试，或直接单测 guard 函数。
- `--dump-config-schema` 在 rc.3 退出码为 1，不能作为 CI 门禁的退出码依据；如要用 schema，需解析 stdout 并容忍已知错误。

---

## 2. KLineChart

### 2.1 版本与许可证

- npm `klinecharts` 的 `latest` 为 **`10.1.1`**，发布于 **2026-11-16T17:21:06.154Z**；不是 beta/rc。v10 的正式版本序列包括 `10.0.0`、`10.0.1`–`10.0.4`、`10.1.0`、`10.1.1`（`10.0.0` 另有 alpha/beta 预发布）。
  - <https://registry.npmjs.org/klinecharts>
  - <https://www.npmjs.com/package/klinecharts/v/10.1.1>
- 许可证 **Apache-2.0**；tarball 内 `LICENSE` 为 Apache-2.0，`NOTICE` 同时声明：
  - `KLineChart Copyright (c) 2019 lihu`
  - `TradingView Lightweight Charts Copyright (с) 2019 TradingView, Inc.`
  - `licenses/LICENSE-lightweight-charts` 也是 Apache-2.0。
- 本机 `npm pack klinecharts@10.1.1` 得到 `/tmp/klinecharts-inspect/klinecharts-10.1.1.tgz`（497,674 bytes），检查后未留在仓库。
- 发行时需保留 `LICENSE` 与 `NOTICE`（Apache-2.0 的 NOTICE 保留义务）；不要把 `@klinecharts/pro`（SolidJS 封装）混入纯 React 包（与 ADR-0005 一致）。

### 2.2 `registerIndicator` 与外部指标结果

**结论**：没有公开 API 允许直接把外部计算好的 `result` 注入指标实例；但官方扩展点 `registerIndicator` + `createIndicator({ name, calc })` 可以让 `calc` 返回外部结果数组，图表按索引渲染。这是“支持渲染外部结果”，不是“直接 setResult”。

关键签名（来自 10.1.1 tarball 的 `dist/index.d.ts`）：

```ts
export declare function registerIndicator<D = unknown, C = unknown, E = unknown>(
  indicator: IndicatorTemplate<D, C, E>,
): void

// IndicatorCalcCallback：返回与 dataList 等长、按索引对齐的 D[]
type IndicatorCalcCallback<D, C, E> = (
  dataList: KLineData[],
  indicator: Indicator<D, C, E>,
) => Promise<D[]> | D[]

interface Indicator<D, C, E> {
  result: D[]                    // 只读结果，由框架持有
  calc: IndicatorCalcCallback<D, C, E>
  // ...
}

type IndicatorTemplate<D, C, E> = /* 省略 result/paneId/yAxisId，name/calc 必填 */
type IndicatorCreate<D, C, E> = /* 省略 result，name 必填，calc 可选 */

createIndicator(value: string | IndicatorCreate, isStack?: boolean): Nullable<string>
overrideIndicator(override: IndicatorCreate): boolean
```

- `dist/index.d.ts:990`：`setDataLoader: (dataLoader: DataLoader) => void;`
- `dist/index.d.ts:1247`：`registerIndicator<D, C, E>(indicator: IndicatorTemplate<D, C, E>): void`
- `createIndicator` 要求该 `name` 已通过 `registerIndicator` 注册：未注册时实现会打印 `indicator not supported, you may need to use registerIndicator` 并返回 `null`。

**最小示例**（已用 TypeScript 5.9.3 对 10.1.1 类型检查通过；未做浏览器渲染验证）：

```ts
import { Chart, registerIndicator } from 'klinecharts'

type LevelPoint = { level?: number }
const externalByTs = new Map<number, LevelPoint>()

// 先注册模板；calc 只作为占位，实际实例会被 createIndicator 的 calc 覆盖
registerIndicator<LevelPoint>({
  name: 'EXTERNAL_LEVELS',
  series: 'price',
  figures: [{ key: 'level', type: 'line' }],
  calc: () => [],
})

export function attach(chart: Chart): void {
  chart.createIndicator({
    name: 'EXTERNAL_LEVELS',
    calc: (dataList) => dataList.map((k) => externalByTs.get(k.timestamp) ?? {}),
  })
}

// 外部结果更新后：用新的 calc 闭包触发重算
export function refresh(chart: Chart): void {
  chart.overrideIndicator({
    name: 'EXTERNAL_LEVELS',
    calc: (dataList) => dataList.map((k) => externalByTs.get(k.timestamp) ?? {}),
  })
}
```

实现约束：

1. `calc` 返回数组必须与 `dataList` **等长、同序**；图表按 `result[dataIndex]` 取值，缺失项会变成 `{}`。前导 warmup 用 `{}`/缺省值，不要缩短数组。
2. `figures[].key` 必须与 `calc` 返回对象的字段名一致；否则图元无值。
3. 更新时换一个新的 `calc` 闭包（或改 `calcParams` / `figures`）再调用 `overrideIndicator`。默认 `shouldUpdate` 会对比 `prev.calc !== current.calc`、`calcParams`、`figures`；仅修改闭包捕获的数组而不换闭包不会触发重算。
4. 这是公开的 `calc` 扩展点，不是读取/篡改 `result` 的私有 hack；不要依赖 `result` 内部结构。
5. **文档与类型不一致**：官方 10.1.1 API 文档写 `calc` 返回 `Record<Timestamp, unknown>`，但 10.1.1 的 `.d.ts` 与打包实现是 `D[]`（`result[dataIndex]`）。以 tarball 的类型与实现为准；升级时先跑类型检查。
   - <https://klinecharts.com/en-US/api/chart/registerIndicator>

### 2.3 `setDataLoader` 分页接口

v10 的 `DataLoader` 可用，且是 v9 `applyNewData` / `updateData` 移除后的官方数据入口：

```ts
setDataLoader(dataLoader: DataLoader): void

type DataLoadType = 'init' | 'forward' | 'backward' | 'update'
type DataLoadMore = boolean | { backward?: boolean; forward?: boolean }

interface DataLoaderGetBarsParams {
  type: DataLoadType
  timestamp: Nullable<number>
  symbol: SymbolInfo
  period: Period
  callback: (data: KLineData[], more?: DataLoadMore) => void
}

interface DataLoader {
  getBars(params: DataLoaderGetBarsParams): void | Promise<void>
  subscribeBar?: (params: { symbol: SymbolInfo; period: Period; callback: (data: KLineData) => void }) => void
  unsubscribeBar?: (params: { symbol: SymbolInfo; period: Period }) => void
}
```

- `getBars` 只有在 symbol、period 与 loader 都设置后才会触发。
- `type:'init'`：`timestamp` 为 `null`，加载初始页（替换）。
- `type:'forward'`：向更早/左侧翻页（prepend）。
- `type:'backward'`：向更新/右侧翻页（append）。
- `callback(data, more)`：`more` 为 `boolean` 或 `{ forward?: boolean; backward?: boolean }`；某一方向为 `false` 时该方向停止继续加载。
- `subscribeBar` / `unsubscribeBar` 用于实时单根更新，可省略；省略时仍可正常分页。
- 文档最后更新 2026-09-12；v9 的 `applyNewData` / `updateData` 在 v10 不再适用。
  - <https://klinecharts.com/en-US/guide/data-integration>

### 2.4 对任务 02 的阻塞结论

**不阻塞**。锁定 `klinecharts@10.1.1`；外部指标通过 `registerIndicator` 的 `calc` 闭包渲染，分页通过 `setDataLoader` 适配 daemon 的 `init/forward/backward` 协议；保留 Apache-2.0 LICENSE/NOTICE。风险是外部结果渲染依赖公开 `calc` 契约，需补一个真实 canvas 的 smoke test，防止后续小版本改变 `result` 索引语义。

---

## 3. 指标库

### 3.1 TA-Lib

| 项 | 结论 |
|---|---|
| C 库版本 | **`0.8.3`**（2026-10-25 发布，非 prerelease） |
| C 库许可证 | **BSD-3-Clause**，`Copyright (c) 1999-2026, Mario Fortier`；保留版权声明、二进制文档声明、不得用作者名背书 |
| Python 封装版本 | **`0.8.3`**（2026-10-28 发布） |
| Python 封装许可证 | **BSD-2-Clause**（仓库 `LICENSE`；PyPI `license` 字段为 null，不能据此判断无许可证） |
| macOS arm64 + CPython 3.14 | **有预编译 wheel**：`ta_lib-0.8.3-cp314-cp314-macosx_14_0_arm64.whl`（2026-10-28T22:44:16） |
| 其他平台 | 同时有 cp314 manylinux2014/musllinux x86_64+aarch64、Windows、cp314t、cp315 wheel |
| 本机实测 | Python 3.14.6，`uv pip install TA-Lib==0.8.3 talipp==2.7.0`；`talib 0.8.3` / `numpy 2.5.5`；`talib.SMA(...)` 正常返回 |

证据：
- <https://github.com/ta-lib/ta-lib/releases/tag/v0.8.3>
- <https://github.com/ta-lib/ta-lib/blob/v0.8.3/LICENSE>
- <https://github.com/ta-lib/ta-lib-python/releases/tag/v0.8.3>
- <https://github.com/ta-lib/ta-lib-python/blob/v0.8.3/LICENSE>
- <https://pypi.org/project/TA-Lib/0.8.3/#files>

**许可证结论**：C 库 BSD-3-Clause 与 Python 封装 BSD-2-Clause 都是宽松许可，可商用/闭源分发；二进制分发需随附版权与许可文本，且不得暗示背书。PyPI 的 `license` 字段为 null 是元数据缺失，以仓库 LICENSE 为准。

### 3.2 talipp

| 项 | 结论 |
|---|---|
| 最新版本 | **`2.7.0`**（2025-09-09 发布） |
| 许可证 | **MIT**（PyPI classifier + 仓库 LICENSE；PyPI `license` 字段同样为 null） |
| 维护状态 | 仓库最后 push 2025-09-09，32 个 open issues，未 archive；约 14 个月无新 release，属于低频维护 |
| Python 版本 | `requires_python >=3.8`；纯 Python wheel `py3-none-any` |
| 本机实测 | Python 3.14.6 下 `talipp.indicators.SMA(3)` 正常；包没有 `__version__` 属性，版本需从 metadata 读取 |

- <https://pypi.org/project/talipp/2.7.0/>
- <https://github.com/nardew/talipp/blob/HEAD/LICENSE>

**结论**：锁定 `talipp==2.7.0`，放在薄适配层后；其增量 API 与 TA-Lib 批量 API 的边界由 Talaria 的 `operator_vectors` 黄金向量约束。维护低频是残余风险，不建议依赖其未发布分支。

### 3.3 Python 3.14 回退结论

**不需要纯 Python/numpy 回退**：macOS arm64 + CPython 3.14 有官方 cp314 wheel，且本机实测两个库都可用。

但 wheel 平台标签是 `macosx_14_0_arm64`，意味着 **macOS 14+**；如果未来要支持 macOS 13 或非 arm64，需重新评估 wheel 覆盖。回退位置记录为：`docs/architecture.md:198`（Python 单端算子）与 `docs/adr/0008-analysis-first-v1-and-deterministic-key-level-engine.md:7`（TA-Lib/talipp 选型）；若真的回退，必须保留 `contracts/operator_vectors/` 的黄金向量并注明实现替换点。

### 3.4 对任务 04 的阻塞结论

**不阻塞**。锁定 `TA-Lib==0.8.3`、`talipp==2.7.0`；Python 3.14.6 + macOS arm64 已验证；若 CI 使用 macOS <14，需另建 wheel 或源码构建，属于环境风险而非当前阻塞。

---

## 4. 关键位参考项目许可证

| 项目 | 许可证证据 | 最近提交 | 结论 / 可复制性 |
|---|---|---|---|
| `smart-money-concepts` (`joshyattridge/smart-money-concepts`) | `LICENSE` 为 MIT，`Copyright (c) 2020 NeuralNine`；目录页 MIT 与文件一致 | `1b62fd6c41e1`，2026-04-03 | **MIT，允许洁净室借鉴算法思想；不直接复制代码/注释/测试** |
| `trendln` (`GregoryMorse/trendln`) | `LICENSE` 为 MIT，`Copyright (c) 2019 Gregory Morse`；PyPI `trendln 0.1.18` 也是 MIT classifier | `82dcb119a667`，2026-04-09 | **MIT，允许洁净室借鉴；不直接复制** |
| `market-structure`（PyPI 包 `market-structure 0.1.0` → `fortunato/pymarket-structure`） | `LICENSE` 为 MIT，`Copyright (c) 2026 Fortunato`；classifier 仅 Python 3.12/3.13 | `3502b8ec9695`，2026-04-18 | **MIT，允许洁净室借鉴；不直接复制，也不作为 Python 3.14 依赖** |
| `etnfrank/support_resistance`（包名 `pricelevels`） | 根目录 **无 LICENSE 文件**；仅 `setup.py:22` 写 `license='wtfpl'` | `9f8f5550624e`，2019-08-29 | **不可复制**：无正式 LICENSE 文件、项目停更、依赖钉死 2019 年版本；仅可阅读思路，不复用代码 |

补充事实：

- `joshyattridge/market-structure` 与 `tysonwu/market-structure` 在 GitHub API 返回 `full_name: null`（不存在）；不要把这两个仓库当作 `market-structure` 的来源。
- `support_resistance` 的依赖为 `scikit-learn==0.21.2`、`pandas==0.25.0`、`ZigZag==0.2.2`、`matplotlib==3.1.1`，与 Python 3.14 不兼容；即使许可证清晰也不应作为运行时依赖。
- 本分支根目录未发现 `LICENSE` / `COPYING` 文件；对外分发前需补 Talaria 自身许可证与第三方 NOTICE（尤其 Apache-2.0 的 KLineChart NOTICE）。

**洁净室记录要求**：只阅读 README/文档/算法描述并记录来源 commit；由未阅读源码的实现者按数学定义重写；不复用变量名、注释、测试向量；若最终确实复制了 MIT 代码片段，必须在 `THIRD_PARTY_NOTICES` 保留版权与 MIT 许可文本。`support_resistance` 不进入该流程，直接标记不可复制。

### 4.1 对任务 10 的阻塞结论

**不阻塞**。三个 MIT 项目可作为关键位算法的参考；`support_resistance` 明确不可复制。实现时需把“参考项目 + commit + 只借鉴思想”的出处写进注释/研究记录，避免后续无法追溯。

---

## 5. 对实现的硬约束清单

1. **DSH 版本**：锁定 `dsh 0.2.2-rc.3`；不得用 npm `latest`（实际指向 `0.2.2-rc.1`）。升级候选 `0.2.2-rc.5` 必须重跑本报告探针。
2. **Python SDK**：锁定 `deepseek-harness-sdk==0.2.2rc2`；显式传 `dsh_home`，用 `dsh_bin` 指向同一 `dsh 0.2.2-rc.3`；SDK 自带 runtime-bin `0.2.2rc2` 只作隔离回退。
3. **每轮用量**：监听 `session/event` 的 `turn/end` 触发，折叠 `turn/start..turn/end` 内的 `assistant/message` / `llm/retry` usage；幂等键 `(dsh_session_id, turn)`；`undefined` 不按 0 入账，走轨迹补账/UNALLOCATED。
4. **用量字段**：`uncachedInputTokens`（未缓存输入）、`cacheReadTokens`（缓存命中）、`cacheWriteTokens`（缓存写）、`outputTokens`（输出）；需要总 prompt 时用 `totalTokens - outputTokens`，避免与 cache 桶重复相加。
5. **单调守卫**：用“挂载 profile + 触发被禁工具”的行为测试/单测验证；`dsh --dump-config` 只能做权限与插件配置快照，不能证明 guard。
6. **dump-config 门禁**：`--profile` 必填；按文本快照（含 `!!js`），不要 JSON.parse；`--dump-config-schema` 在 rc.3 退出码为 1，需解析 stdout 且容忍已知 `/198..201` 错误。
7. **KLineChart**：`klinecharts@10.1.1`；外部指标用 `registerIndicator` + `createIndicator({name, calc})`；`calc` 返回与 `dataList` 等长数组；更新时换新闭包并 `overrideIndicator`。
8. **分页**：`setDataLoader` 的 `init/forward/backward/update` 与 `callback(data, more)`；daemon 必须按方向返回 `more`，不要再用 v9 的 `applyNewData`/`updateData`。
9. **Python 指标**：`TA-Lib==0.8.3` + `talipp==2.7.0`；Python 3.14/macOS arm64 wheel 已验证；保留 BSD/MIT 许可文本。
10. **许可证**：KLineChart 保留 Apache-2.0 LICENSE/NOTICE；TA-Lib 保留 BSD-3/BSD-2；talipp 保留 MIT；MIT 参考项目按洁净室；`support_resistance` 不复制。

---

## 6. 需要回改架构规范或 ADR 的位置

本次只记录位置，未修改 `docs/architecture.md` 或 `docs/adr/`。

| 位置 | 现状 / 问题 | 建议回改 |
|---|---|---|
| `docs/architecture.md:403`（§5.3 第 ③ 项） | 声称 `dsh --dump-config` 可快照“权限与单调执行守卫”；实测 dump 中 guard 计数为 0，守卫是运行时注册的函数 | **必须回改**：拆成“权限/插件配置快照”+“守卫行为测试”；或删除“守卫快照”表述，避免契约门禁给出虚假保证 |
| `docs/architecture.md:310`（§3.6 用量采集通道） | 要求 `dsh-bridge` 每轮结束上报 `(session_id, turn_id, 输入/缓存/输出)`；但 `turn/end` 无 usage 字段，`deriveTurnTokenUsage` 非公开 API，`turn` 是会话内序号 | 建议补一句：bridge 需折叠 turn 内 attempt 事件；幂等键为 `(session_id, turn)`；`undefined` 不得按 0 入账 |
| `docs/architecture.md:377`、`:380`（§5.1） | 写“官方 `deepseek-harness-sdk`（stdio）”，未锁版本，也未说明 SDK 自带 runtime rc.2 与桌面 rc.3 的差异 | 建议补：SDK `0.2.2rc2` + `dsh_bin` 指向 `0.2.2-rc.3`；显式 `dsh_home`；升级需重验 |
| `docs/adr/0001-dual-plane-dsh-and-python-core.md:7` | 仅写 `0.2.x` Developer Preview，无精确锁定 | 建议校准：注明本报告锁定的 rc.3 / SDK rc.2 组合；不改变 ADR 结论 |
| `docs/adr/0005-fast-path-bypass-and-decoupled-chart-workbench.md:7` | 写 `KLineChart v10`，未写具体版本与外部结果渲染方式 | 建议校准：`10.1.1`；外部结果经 `calc` 渲染，不是直接注入 `result` |
| `docs/adr/0008-analysis-first-v1-and-deterministic-key-level-engine.md:7` | 写复用 `TA-Lib` / `talipp`，未锁版本 | 建议校准：`TA-Lib 0.8.3`（C BSD-3 / Python BSD-2）、`talipp 2.7.0`（MIT）；Python 3.14 wheel 已验证 |
| 其他位置 | 无（KLineChart 分页、许可证结论与现有 §4.1/ADR-0005 不冲突） | 无 |

---

## 7. 证据与复现命令

以下命令均在 `/tmp` 或只读查询中执行，未向仓库写入依赖。

### 7.1 DSH

```bash
dsh --version
# 0.2.2-rc.3

curl -s 'https://api.github.com/repos/deepseek-ai/deepseek-harness/releases?per_page=100' \
  | jq '{total:length, stable:[.[]|select(.prerelease==false)|.tag_name], latest:.[0].tag_name}'
# {"total":41,"stable":[],"latest":"dsh-v0.2.2-rc.5"}

DSH_HOME=/tmp/dshhome-probe dsh --dump-config
# exit 1, stderr: error: --profile <name> is required

DSH_HOME=/tmp/dshhome-probe dsh --dump-config --profile web > /tmp/dump-web.yaml
# exit 0; 46,413 bytes; 202 个 "- id:"; 46 处 "!!js"

DSH_HOME=/tmp/dshhome-probe dsh --dump-config --profile sdk | head -1
# # == @deepseek-ai/dsh-base, patched by @deepseek-ai/dsh-sdk-app

DSH_HOME=/tmp/dshhome-probe dsh --dump-config-schema --profile web > /tmp/schema.json 2>/tmp/schema.err
# exit 1; stdout 为合法 JSON（946,214 bytes）；stderr 含 /198../201 Loader tree carrier 错误
grep -ci guard /tmp/dump-web.yaml
# 0
```

SDK 握手（Python 3.14，venv `/tmp/dshsdkvenv`）：

```text
deepseek-harness-sdk 0.2.2rc2
deepseek-harness-runtime-bin 0.2.2rc2
dsh_bin=/opt/homebrew/bin/dsh, dsh_home=/tmp/dshhome-probe, cwd=/tmp
=> INIT_OK True
=> CLOSED_OK
```

### 7.2 KLineChart

```bash
mkdir -p /tmp/klinecharts-inspect && cd /tmp/klinecharts-inspect
npm pack klinecharts@10.1.1
# klinecharts-10.1.1.tgz, 497,674 bytes
tar -xzf klinecharts-10.1.1.tgz
grep -n 'setDataLoader\|registerIndicator' package/dist/index.d.ts
# 990: setDataLoader: (dataLoader: DataLoader) => void;
# 1247: export declare function registerIndicator<D = unknown, C = unknown, E = unknown>(...)
```

最小示例的类型检查：

```bash
# /tmp/klc-check/example.ts 使用 10.1.1 的 dist/index.d.ts
npx --yes -p typescript@5.9.3 tsc --noEmit --strict \
  --target ES2020 --module esnext --moduleResolution bundler example.ts
# EXIT:0
```

### 7.3 TA-Lib / talipp

```bash
uv pip install TA-Lib==0.8.3 talipp==2.7.0   # /tmp/talkv, Python 3.14.6
python -c "import talib, talipp, numpy; print(talib.__version__, numpy.__version__)"
# 0.8.3 2.5.5
# talib.SMA(...) -> 4.0; talipp.indicators.SMA(3) -> 3.0
```

wheel 证据：<https://pypi.org/project/TA-Lib/0.8.3/#files>（`ta_lib-0.8.3-cp314-cp314-macosx_14_0_arm64.whl`）。

### 7.4 许可证

```bash
curl -s https://raw.githubusercontent.com/ta-lib/ta-lib/v0.8.3/LICENSE | head -3
# Copyright (c) 1999-2026, Mario Fortier

curl -s https://raw.githubusercontent.com/ta-lib/ta-lib-python/v0.8.3/LICENSE | head -1
# # BSD 2-Clause License

curl -s https://api.github.com/repos/joshyattridge/smart-money-concepts | jq -r '.license.spdx_id'
# MIT
curl -s https://api.github.com/repos/GregoryMorse/trendln | jq -r '.license.spdx_id'
# MIT
curl -s https://api.github.com/repos/fortunato/pymarket-structure | jq -r '.license.spdx_id'
# MIT
```

`support_resistance`：根目录无 LICENSE；`setup.py:22` 为 `license='wtfpl'`。链接：<https://github.com/etnfrank/support_resistance/blob/9f8f5550624e/setup.py#L22>。

---

## 8. 遗留风险

1. **DSH 无 stable**：`0.2.2-rc.3` / SDK `0.2.2rc2` 只是当前验证组合；rc.5 与未来 rc 均可能破坏协议，必须锁定精确版本并在升级时重跑 dump/guard/usage 探针。
2. **Python SDK 与桌面 runtime 版本不完全一致**：SDK 自带 rc.2，桌面是 rc.3；本次只验证 initialize/close 握手，完整 `session/prompt`、工具调用、`session.event` 流未验证。若要求严格同版本，需把桌面也降到 rc.2 或等 SDK 发布 rc.3。
3. **用量折叠无公开 helper**：TS/Python 两端各自实现会漂移；`deriveTurnTokenUsage` 的 `undefined` 语义（缺事件/计数不安全/总数冲突）必须在账本层正确处理，不能按 0 入账。
4. **dump-config 不能证明守卫**：架构 §5.3 ③ 的现有表述会给出虚假保证；必须补行为测试。
5. **`--dump-config-schema` 在 rc.3 退出码为 1**：即使 stdout 是合法 JSON，也不能直接作为 CI 成功条件；需白名单已知错误或改用 `--dump-config` 文本快照。
6. **ui-slots 版本/许可证漂移**：npm `latest` 仍指 `0.0.1-rc.1`（BSD-3），`next` 才是 `0.2.2-rc.5`（MIT）；若使用 ui-slots，必须锁 0.2.2 线并重核 LICENSE。
7. **KLineChart 文档与类型不一致**：10.1.1 文档的 `calc` 返回类型是旧写法；实现以 `.d.ts`/源码为准，并补真实 canvas 渲染 smoke test，避免升级时索引语义变化。
8. **TA-Lib wheel 平台限制**：`macosx_14_0_arm64` 只覆盖 macOS 14+ arm64；CI/用户机器若低于该平台需源码构建或另选方案。
9. **talipp 维护低频**：2025-09 后无 release；锁定 2.7.0 并封装适配层，升级前跑 `operator_vectors`。
10. **许可证/合规**：Talaria 根目录暂无 LICENSE；对外分发前需补自身许可证与第三方 NOTICE；`support_resistance` 无正式 LICENSE 文件，已标记不可复制。

---

## 9. 参考入口

- DSH 仓库：<https://github.com/deepseek-ai/deepseek-harness>
- DSH releases：<https://github.com/deepseek-ai/deepseek-harness/releases>
- DSH 生命周期：<https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/agent-lifecycle.md>
- DSH token-meter：<https://github.com/deepseek-ai/deepseek-harness/tree/master/packages/llm/token-meter>
- DSH tools/guard：<https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/core/tools/src/contracts.ts>
- DSH SDK 协议：<https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/sdk/protocol/src/types.ts>
- Python SDK：<https://pypi.org/project/deepseek-harness-sdk/>
- KLineChart npm：<https://www.npmjs.com/package/klinecharts>
- KLineChart 仓库：<https://github.com/klinecharts/KLineChart>
- KLineChart `registerIndicator` 文档：<https://klinecharts.com/en-US/api/chart/registerIndicator>
- KLineChart 数据集成：<https://klinecharts.com/en-US/guide/data-integration>
- TA-Lib C：<https://github.com/ta-lib/ta-lib>
- TA-Lib Python：<https://github.com/ta-lib/ta-lib-python>
- talipp：<https://github.com/nardew/talipp>
- smart-money-concepts：<https://github.com/joshyattridge/smart-money-concepts>
- trendln：<https://github.com/GregoryMorse/trendln>
- market-structure：<https://github.com/fortunato/pymarket-structure>
- support_resistance：<https://github.com/etnfrank/support_resistance>
