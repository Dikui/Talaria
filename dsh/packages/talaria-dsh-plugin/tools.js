import { readFileSync } from 'node:fs'

import {
  AGENT_ANNOTATION_OUTPUT_SCHEMA,
  ALERT_DRAFT_OUTPUT_SCHEMA,
  CHART_VIEW_OUTPUT_SCHEMA,
  createBridge,
  INDICATOR_BUNDLE_OUTPUT_SCHEMA,
  KEY_LEVEL_SET_OUTPUT_SCHEMA,
  OPERATOR_OUTPUT_SCHEMA,
  renderAgentAnnotation,
  renderAlertDraft,
  renderChartView,
  renderIndicators,
  renderKeyLevels,
  renderOperator,
  renderSnapshot,
  SNAPSHOT_OUTPUT_SCHEMA,
} from './bridge.js'

export const TOOL_NAMES = [
  'get_snapshot',
  'get_indicators',
  'get_key_levels',
  'compute_operator',
  'get_chart_view',
  'write_agent_overlay',
  'create_alert',
]

const WRITE_TOOLS = new Set(['write_agent_overlay', 'create_alert'])

const toolSchemas = Object.fromEntries(
  TOOL_NAMES.map((toolName) => [
    toolName,
    JSON.parse(readFileSync(new URL(`./${toolName}.tool.json`, import.meta.url), 'utf8')),
  ]),
)

export const name = '@talaria/dsh-plugin/tools'
export const inject = ['tools']

function outputOf(tool) {
  if (tool === 'get_snapshot') return [SNAPSHOT_OUTPUT_SCHEMA, renderSnapshot]
  if (tool === 'get_indicators') return [INDICATOR_BUNDLE_OUTPUT_SCHEMA, renderIndicators]
  if (tool === 'get_key_levels') return [KEY_LEVEL_SET_OUTPUT_SCHEMA, renderKeyLevels]
  if (tool === 'compute_operator') return [OPERATOR_OUTPUT_SCHEMA, renderOperator]
  if (tool === 'write_agent_overlay') return [AGENT_ANNOTATION_OUTPUT_SCHEMA, renderAgentAnnotation]
  if (tool === 'create_alert') return [ALERT_DRAFT_OUTPUT_SCHEMA, renderAlertDraft]
  return [CHART_VIEW_OUTPUT_SCHEMA, renderChartView]
}

function executeOf(bridge, tool) {
  if (tool === 'get_snapshot') return (args, signal) => bridge.createSnapshot(args, signal)
  if (tool === 'get_indicators') return (args, signal) => bridge.getIndicators(args, signal)
  if (tool === 'get_key_levels') return (args, signal) => bridge.getKeyLevels(args, signal)
  if (tool === 'compute_operator') return (args, signal) => bridge.computeOperator(args, signal)
  if (tool === 'write_agent_overlay') return (args, signal) => bridge.writeAgentOverlay(args, signal)
  if (tool === 'create_alert') return (args, signal) => bridge.createAlertDraft(args, signal)
  return (args, signal) => bridge.getChartView(args, signal)
}

function descriptionOf(tool, baseDescription) {
  if (tool === 'get_snapshot') {
    return `${baseDescription} 只读；返回的 snapshot_id 必须用于后续所有读取。`
  }
  if (WRITE_TOOLS.has(tool)) {
    return `${baseDescription} snapshot_id 必须来自 get_snapshot。`
  }
  return `${baseDescription} 只读；snapshot_id 必须来自 get_snapshot。`
}

/**
 * 在 talaria-desk 预设作用域内注册 Talaria 的 7 个分析与受门控草案工具，
 * 避免注入到非 Talaria 通用会话的上下文中。
 */
export function apply(ctx, config = {}) {
  const { credentialFile, serviceDescriptor, dataDir, baseUrl, token, cwd } = config
  const bridge = createBridge({ credentialFile, serviceDescriptor, dataDir, baseUrl, token, cwd })

  for (const tool of TOOL_NAMES) {
    const schema = toolSchemas[tool]
    const [outputSchema, render] = outputOf(tool)
    const dispose = ctx.tools.register({
      name: schema.name,
      description: descriptionOf(tool, schema.description),
      parameters: schema.inputSchema,
      output: { schema: outputSchema, render },
      execute: (args, exec) => executeOf(bridge, tool)(args, exec.signal),
    })
    if (typeof dispose === 'function') {
      ctx.effect?.(() => dispose)
    }
  }
}
