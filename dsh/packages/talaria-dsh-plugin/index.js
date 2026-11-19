import { readFileSync } from 'node:fs'

import {
  CHART_VIEW_OUTPUT_SCHEMA,
  createBridge,
  INDICATOR_BUNDLE_OUTPUT_SCHEMA,
  KEY_LEVEL_SET_OUTPUT_SCHEMA,
  OPERATOR_OUTPUT_SCHEMA,
  renderChartView,
  renderIndicators,
  renderKeyLevels,
  renderOperator,
  renderSnapshot,
  SNAPSHOT_OUTPUT_SCHEMA,
} from './bridge.js'
import { createDeskGuard } from './guard.js'

const TOOL_NAMES = [
  'get_snapshot',
  'get_indicators',
  'get_key_levels',
  'compute_operator',
  'get_chart_view',
]

const toolSchemas = Object.fromEntries(
  TOOL_NAMES.map((name) => [
    name,
    JSON.parse(readFileSync(new URL(`./${name}.tool.json`, import.meta.url), 'utf8')),
  ]),
)

export const name = '@talaria/dsh-plugin'
export const inject = ['tools', 'sessions', 'webServer']

const CONSULT_PATH = '/talaria/consult'
const CONSULT_SESSION_PREFIX = 'talaria-consult-'

function outputOf(tool) {
  if (tool === 'get_snapshot') return [SNAPSHOT_OUTPUT_SCHEMA, renderSnapshot]
  if (tool === 'get_indicators') return [INDICATOR_BUNDLE_OUTPUT_SCHEMA, renderIndicators]
  if (tool === 'get_key_levels') return [KEY_LEVEL_SET_OUTPUT_SCHEMA, renderKeyLevels]
  if (tool === 'compute_operator') return [OPERATOR_OUTPUT_SCHEMA, renderOperator]
  return [CHART_VIEW_OUTPUT_SCHEMA, renderChartView]
}

function executeOf(bridge, tool) {
  if (tool === 'get_snapshot') return (args, signal) => bridge.createSnapshot(args, signal)
  if (tool === 'get_indicators') return (args, signal) => bridge.getIndicators(args, signal)
  if (tool === 'get_key_levels') return (args, signal) => bridge.getKeyLevels(args, signal)
  if (tool === 'compute_operator') return (args, signal) => bridge.computeOperator(args, signal)
  return (args, signal) => bridge.getChartView(args, signal)
}

function sendJson(response, status, payload) {
  response.writeHead(status, {
    'content-type': 'application/json; charset=utf-8',
    'cache-control': 'no-store',
  })
  response.end(JSON.stringify(payload))
}

async function readJsonBody(request, limit = 1024 * 1024) {
  const chunks = []
  let size = 0
  for await (const chunk of request) {
    size += chunk.length
    if (size > limit) throw new Error('请求体过大')
    chunks.push(chunk)
  }
  if (chunks.length === 0) return {}
  return JSON.parse(Buffer.concat(chunks).toString('utf8'))
}

function sessionEvents(session) {
  const events = []
  const end = Number(session?.seq ?? 0)
  for (let seq = 0; seq < end; seq += 1) {
    const event = session.eventAt(seq)
    if (event) events.push(event)
  }
  return events
}

function sessionIdOf(session) {
  return session?.id ?? session?.header?.id ?? undefined
}

function latestTurn(events) {
  for (let index = events.length - 1; index >= 0; index -= 1) {
    const event = events[index]
    if (event.type === 'turn/end' || event.type === 'turn/start') {
      return typeof event.data?.turn === 'number' ? event.data.turn : undefined
    }
    if (
      event.type === 'assistant/message'
      && typeof event.data?.turn === 'number'
    ) {
      return event.data.turn
    }
  }
  return undefined
}

function turnUsage(events, turn) {
  if (typeof turn !== 'number') return null
  const usages = events
    .filter(
      (event) =>
        event.type === 'assistant/message'
        && event.data?.turn === turn
        && event.data?.usage,
    )
    .map((event) => event.data.usage)
  if (usages.length === 0) return null
  const sumField = (key) =>
    usages.every((usage) => typeof usage[key] === 'number')
      ? usages.reduce((total, usage) => total + usage[key], 0)
      : null
  return {
    input_tokens: sumField('inputTokens'),
    cached_input_tokens: sumField('cacheReadTokens'),
    output_tokens: sumField('outputTokens'),
  }
}

async function waitForNewTurn(session, baseline, timeoutMs) {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    const turn = latestTurn(sessionEvents(session))
    if (
      typeof turn === 'number'
      && (typeof baseline !== 'number' || turn > baseline)
    ) {
      return true
    }
    await new Promise((resolve) => setTimeout(resolve, 80))
  }
  return false
}

function lastAssistantText(events) {
  for (let index = events.length - 1; index >= 0; index -= 1) {
    const event = events[index]
    if (event.type !== 'assistant/message') continue
    const content = event.data?.message?.content
    if (!Array.isArray(content)) continue
    const text = content
      .filter((block) => block?.type === 'text' && typeof block.text === 'string')
      .map((block) => block.text)
      .join('\n')
      .trim()
    if (text.length > 0) return text
  }
  return ''
}

/**
 * talaria-desk Profile 的宿主插件：
 * - 注册五个只读工具，全部经回环 HTTP + Bearer 调用 Python 守护进程；
 * - 挂载单调执行守卫，拦截非白名单工具与 fork 历史写操作重放；
 * - 提供 /talaria/consult：异步运行一轮模型，把文本与 Token 用量回报给守护进程。
 */
export function apply(ctx, config = {}) {
  const bridge = createBridge({
    credentialFile: config.credentialFile,
    baseUrl: config.baseUrl,
    token: config.token,
  })

  for (const tool of TOOL_NAMES) {
    const schema = toolSchemas[tool]
    const [outputSchema, render] = outputOf(tool)
    ctx.tools.register({
      name: schema.name,
      description:
        tool === 'get_snapshot'
          ? `${schema.description} 只读；返回的 snapshot_id 必须用于后续所有读取。`
          : `${schema.description} 只读；snapshot_id 必须来自 get_snapshot。`,
      parameters: schema.inputSchema,
      output: { schema: outputSchema, render },
      execute: (args, exec) => executeOf(bridge, tool)(args, exec.signal),
    })
  }

  const getSession = (exec) =>
    exec.agent?.session ?? (exec.agent ? ctx.sessions.get(exec.agent.id) : undefined)
  ctx.tools.guard(createDeskGuard({ getSession }))

  const consultations = new Map()

  function modelSelection() {
    if (config.provider && config.model) {
      return { provider: config.provider, model: config.model }
    }
    const selection = ctx.get?.('agentDefaultModel')?.currentSelection?.()
    if (selection?.provider && selection?.model) {
      return { provider: selection.provider, model: selection.model }
    }
    return undefined
  }

  async function reportUsage(consultationId, sessionId, turn, events) {
    const usage = turnUsage(events, turn)
    if (!sessionId || typeof turn !== 'number') return
    const payload = {
      dsh_session_id: sessionId,
      dsh_turn_id: String(turn),
      input_tokens: usage?.input_tokens ?? null,
      cached_input_tokens: usage?.cached_input_tokens ?? null,
      output_tokens: usage?.output_tokens ?? null,
      source: usage ? 'host' : 'missing',
    }
    try {
      await bridge.reportUsage(payload)
    } catch (error) {
      console.warn(`[talaria] 会诊 ${consultationId} 的 Token 上报失败：${error.message}`)
    }
  }

  async function runConsultation({ consultationId, prompt, timeoutMs }) {
    const selection = modelSelection()
    const agents = ctx.get?.('agents')
    if (!selection || !agents) {
      await bridge
        .submitResult(consultationId, {
          abstain: {
            reason_code: 'HOST_OFFLINE',
            message: '协同宿主未配置模型或 agent 服务不可用；本轮未调用模型。',
            evidence_refs: [],
          },
        })
        .catch(() => {})
      return
    }

    const sessionId = `${CONSULT_SESSION_PREFIX}${consultationId}`
    let handle
    let text = ''
    let timedOut = false
    try {
      handle = await agents.create({
        sessionId,
        agentOptions: selection,
        setup: () => {},
      })
      const baselineTurn = latestTurn(sessionEvents(handle.agent.session))
      await handle.agent.followup({
        content: [{ type: 'text', text: prompt }],
        source: { kind: 'user' },
      })
      // followup 只是排队；先等到新回合真正开始，再等整轮静默，避免把
      // “尚未开始”误判为空回复。
      const started = await waitForNewTurn(
        handle.agent.session,
        baselineTurn,
        timeoutMs,
      )
      if (!started) {
        timedOut = true
        handle.agent.cancel({ kind: 'user' })
      } else {
        const idle = handle.agent.whenIdle().then(() => 'idle')
        const timeout = new Promise((resolve) => {
          const timer = setTimeout(() => resolve('timeout'), timeoutMs)
          timer.unref?.()
        })
        if ((await Promise.race([idle, timeout])) === 'timeout') {
          timedOut = true
          handle.agent.cancel({ kind: 'user' })
          await Promise.race([
            handle.agent.whenIdle().catch(() => {}),
            new Promise((resolve) => setTimeout(resolve, 2000)),
          ])
        }
      }
      const events = sessionEvents(handle.agent.session)
      text = lastAssistantText(events)
      const turn = latestTurn(events)
      if (timedOut) {
        await bridge.submitResult(consultationId, {
          abstain: {
            reason_code: 'MODEL_TIMEOUT',
            message: `协同会诊超过 ${Math.round(timeoutMs / 1000)} 秒未完成；已中止，不输出伪结论。`,
            evidence_refs: [],
          },
          dsh_session_id: sessionIdOf(handle.agent.session) ?? sessionId,
          ...(typeof turn === 'number' ? { dsh_turn_id: String(turn) } : {}),
        })
      } else if (text.length > 0) {
        await bridge.submitResult(consultationId, {
          text,
          dsh_session_id: sessionIdOf(handle.agent.session) ?? sessionId,
          ...(typeof turn === 'number' ? { dsh_turn_id: String(turn) } : {}),
        })
      } else {
        await bridge.submitResult(consultationId, {
          error: '宿主模型没有返回任何文本',
          dsh_session_id: sessionIdOf(handle.agent.session) ?? sessionId,
          ...(typeof turn === 'number' ? { dsh_turn_id: String(turn) } : {}),
        })
      }
      await reportUsage(
        consultationId,
        sessionIdOf(handle.agent.session) ?? sessionId,
        turn,
        events,
      )
    } catch (error) {
      console.warn(`[talaria] 会诊 ${consultationId} 失败：${error.message}`)
      await bridge
        .submitResult(consultationId, {
          abstain: {
            reason_code: 'HOST_OFFLINE',
            message: `协同宿主执行失败：${error.message}`,
            evidence_refs: [],
          },
        })
        .catch(() => {})
    } finally {
      consultations.delete(consultationId)
      if (handle) {
        await handle.dispose().catch(() => {})
      }
    }
  }

  async function handleConsult(request, response) {
    if (request.method !== 'POST') {
      sendJson(response, 405, { code: 'METHOD_NOT_ALLOWED', message: '仅支持 POST。' })
      return
    }
    let body
    try {
      body = await readJsonBody(request)
    } catch (error) {
      sendJson(response, 400, { code: 'INVALID_JSON', message: error.message })
      return
    }
    const consultationId = body?.consultation_id
    const prompt = body?.prompt
    if (typeof consultationId !== 'string' || consultationId.length === 0) {
      sendJson(response, 400, { code: 'INVALID_REQUEST', message: '缺少 consultation_id。' })
      return
    }
    if (typeof prompt !== 'string' || prompt.length === 0) {
      sendJson(response, 400, { code: 'INVALID_REQUEST', message: '缺少 prompt。' })
      return
    }
    if (consultations.has(consultationId)) {
      sendJson(response, 409, { code: 'CONSULTATION_BUSY', message: '同一会诊已在运行。' })
      return
    }
    const timeoutMs = Math.min(
      Math.max(Number(body?.timeout_ms) || 120_000, 10_000),
      300_000,
    )
    consultations.set(consultationId, true)
    sendJson(response, 202, { accepted: true, consultation_id: consultationId })
    void runConsultation({ consultationId, prompt, timeoutMs })
  }

  // 浏览器嵌入所需的回环守护进程地址与 Bearer token 只经同源 bootstrap 下发。
  ctx.effect(() =>
    ctx.webServer.register({
      kind: 'exact',
      path: '/talaria/bootstrap',
      handler(_request, response) {
        const credentialFile = process.env.TALARIA_DSH_BRIDGE
        try {
          const descriptor = JSON.parse(readFileSync(credentialFile, 'utf8'))
          response.writeHead(200, {
            'content-type': 'application/json; charset=utf-8',
            'cache-control': 'no-store',
          })
          response.end(
            JSON.stringify({
              base_url: descriptor.base_url,
              token: descriptor.token,
            }),
          )
        } catch (error) {
          response.writeHead(503, { 'content-type': 'application/json; charset=utf-8' })
          response.end(
            JSON.stringify({
              code: 'TALARIA_BOOTSTRAP_UNAVAILABLE',
              message: `无法读取 Talaria 桥接凭据：${error.message}`,
            }),
          )
        }
      },
    }),
  )

  ctx.effect(() =>
    ctx.webServer.register({
      kind: 'exact',
      path: CONSULT_PATH,
      handler(request, response) {
        handleConsult(request, response).catch((error) => {
          if (!response.headersSent) {
            sendJson(response, 500, { code: 'TALARIA_CONSULT_ERROR', message: error.message })
          }
        })
      },
    }),
  )
}
