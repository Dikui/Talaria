import { randomUUID } from 'node:crypto'
import { mkdirSync, readFileSync, renameSync, rmSync, writeFileSync } from 'node:fs'
import { dirname, join } from 'node:path'

import { createBridge } from './bridge.js'
import { createDeskGuard } from './guard.js'

export const name = '@talaria/dsh-plugin'
export const inject = ['tools', 'sessions', 'webServer', 'agentPresets', 'agentDefaultModel', 'agents', 'connection']

const CONSULT_PATH = '/talaria/consult'
const CONSULT_SESSION_PREFIX = 'talaria-consult-'
const BRIEFING_PATH = '/talaria/briefing'
const BRIEFING_SESSION_PREFIX = 'talaria-briefing-'
const DESK_PRESET = 'talaria-desk'

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

function advertiseDesktop(ctx, credentialFile) {
  const directory = dirname(credentialFile)
  const path = join(directory, 'dsh-desktop.json')
  const instance = randomUUID()
  const temporary = join(directory, `.dsh-desktop-${instance}.tmp`)
  const payload = JSON.stringify({ base_url: ctx.webServer.getOrigin(), pid: process.pid, instance })
  mkdirSync(directory, { recursive: true, mode: 0o700 })
  try {
    writeFileSync(temporary, payload, { mode: 0o600, flag: 'wx' })
    renameSync(temporary, path)
  } finally {
    rmSync(temporary, { force: true })
  }
  return () => {
    try {
      if (readFileSync(path, 'utf8') === payload) rmSync(path, { force: true })
    } catch (error) {
      if (error.code !== 'ENOENT') throw error
    }
  }
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
 * Talaria 的 DSH 协同插件（ADR-0023）：
 * - 由子插件 @talaria/dsh-plugin/tools 在 talaria-desk 预设内按会话挂载只读分析与受控标注/预警草案工具；
 * - 挂载单调执行守卫，拦截非白名单工具与 fork 历史写操作重放；
 * - 仅按 service-descriptor.json 只读连接外部 talaria-data，提供 /talaria/bootstrap、/talaria/consult 与 /talaria/briefing，
 *   不再托管工作台主界面或 Resident Service 生命周期。
 */
export function apply(ctx, config = {}) {
  const bridge = createBridge({
    credentialFile: config.credentialFile,
    serviceDescriptor: config.serviceDescriptor,
    dataDir: config.dataDir,
    baseUrl: config.baseUrl,
    token: config.token,
    cwd: config.cwd,
  })

  const getSession = (exec) =>
    exec.agent?.session ?? (exec.agent ? ctx.sessions.get(exec.agent.id) : undefined)
  const guard = createDeskGuard({ getSession })
  ctx.tools.guard((exec) => {
    const session = getSession(exec)
    const id = sessionIdOf(session) ?? exec.agent?.id
    if (id?.startsWith(CONSULT_SESSION_PREFIX) || id?.startsWith(BRIEFING_SESSION_PREFIX)
      || (session && ctx.agentPresets?.presetForSession(session) === DESK_PRESET)) {
      return guard(exec)
    }
    return undefined
  })

  const consultations = new Map()
  const briefings = new Map()
  const setupAgent = async (agentCtx) => {
    await ctx.agentPresets?.mount(agentCtx, DESK_PRESET)
  }

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

  function usageReportFor(sessionId, turn, events) {
    if (!sessionId || typeof turn !== 'number') return undefined
    const usage = turnUsage(events, turn)
    return {
      dsh_session_id: sessionId,
      dsh_turn_id: String(turn),
      input_tokens: usage?.input_tokens ?? null,
      cached_input_tokens: usage?.cached_input_tokens ?? null,
      output_tokens: usage?.output_tokens ?? null,
      source: usage ? 'host' : 'missing',
    }
  }

  async function reportUsage(consultationId, sessionId, turn, events) {
    const payload = usageReportFor(sessionId, turn, events)
    if (!payload) return
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
        meta: { agentPreset: DESK_PRESET },
        agentOptions: selection,
        setup: setupAgent,
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


  async function runBriefing({ briefingId, prompt, timeoutMs }) {
    const selection = modelSelection()
    const agents = ctx.get?.('agents')
    if (!selection || !agents) {
      await bridge
        .submitBriefingResult(briefingId, {
          abstain: {
            reason_code: 'HOST_OFFLINE',
            message: '协同宿主未配置模型或 agent 服务不可用；本轮未调用模型。',
            evidence_refs: [],
          },
        })
        .catch(() => {})
      return
    }

    const sessionId = `${BRIEFING_SESSION_PREFIX}${briefingId}`
    let handle
    let text = ''
    let timedOut = false
    try {
      handle = await agents.create({
        sessionId,
        meta: { agentPreset: DESK_PRESET },
        agentOptions: selection,
        setup: setupAgent,
      })
      const baselineTurn = latestTurn(sessionEvents(handle.agent.session))
      await handle.agent.followup({
        content: [{ type: 'text', text: prompt }],
        source: { kind: 'user' },
      })
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
      const dshSessionId = sessionIdOf(handle.agent.session) ?? sessionId
      const usage = usageReportFor(dshSessionId, turn, events)
      const base = {
        dsh_session_id: dshSessionId,
        ...(typeof turn === 'number' ? { dsh_turn_id: String(turn) } : {}),
        ...(usage ? { usage } : {}),
      }
      if (timedOut) {
        await bridge.submitBriefingResult(briefingId, {
          abstain: {
            reason_code: 'MODEL_TIMEOUT',
            message: `协同简报超过 ${Math.round(timeoutMs / 1000)} 秒未完成；已中止，不输出伪结论。`,
            evidence_refs: [],
          },
          ...base,
        })
      } else if (text.length > 0) {
        await bridge.submitBriefingResult(briefingId, { text, ...base })
      } else {
        await bridge.submitBriefingResult(briefingId, {
          error: '宿主模型没有返回任何文本',
          ...base,
        })
      }
    } catch (error) {
      console.warn(`[talaria] 简报 ${briefingId} 失败：${error.message}`)
      await bridge
        .submitBriefingResult(briefingId, {
          abstain: {
            reason_code: 'HOST_OFFLINE',
            message: `协同宿主执行失败：${error.message}`,
            evidence_refs: [],
          },
        })
        .catch(() => {})
    } finally {
      briefings.delete(briefingId)
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
    if (request.headers?.['content-type'] !== 'application/json') {
      sendJson(response, 415, { code: 'INVALID_CONTENT_TYPE', message: '需要 application/json。' })
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


  async function handleBriefing(request, response) {
    if (request.method !== 'POST') {
      sendJson(response, 405, { code: 'METHOD_NOT_ALLOWED', message: '仅支持 POST。' })
      return
    }
    if (request.headers?.['content-type'] !== 'application/json') {
      sendJson(response, 415, { code: 'INVALID_CONTENT_TYPE', message: '需要 application/json。' })
      return
    }
    let body
    try {
      body = await readJsonBody(request)
    } catch (error) {
      sendJson(response, 400, { code: 'INVALID_JSON', message: error.message })
      return
    }
    const briefingId = body?.briefing_id
    const prompt = body?.prompt
    if (typeof briefingId !== 'string' || briefingId.length === 0) {
      sendJson(response, 400, { code: 'INVALID_REQUEST', message: '缺少 briefing_id。' })
      return
    }
    if (typeof prompt !== 'string' || prompt.length === 0) {
      sendJson(response, 400, { code: 'INVALID_REQUEST', message: '缺少 prompt。' })
      return
    }
    if (briefings.has(briefingId)) {
      sendJson(response, 409, { code: 'BRIEFING_BUSY', message: '同一简报已在运行。' })
      return
    }
    const timeoutMs = Math.min(
      Math.max(Number(body?.timeout_ms) || 90_000, 10_000),
      300_000,
    )
    briefings.set(briefingId, true)
    sendJson(response, 202, { accepted: true, briefing_id: briefingId })
    void runBriefing({ briefingId, prompt, timeoutMs })
  }

  // 浏览器嵌入所需的回环守护进程地址与 Bearer token 只经同源 bootstrap 下发。
  ctx.effect(() =>
    ctx.webServer.register({
      kind: 'exact',
      path: '/talaria/bootstrap',
      handler(request, response) {
        const rejection = ctx.connection.requestRejection(request)
        if (rejection !== undefined) {
          sendJson(response, rejection, { code: 'FORBIDDEN', message: '需要 DSH 会话。' })
          return
        }
        if (request.method !== 'GET') {
          sendJson(response, 405, { code: 'METHOD_NOT_ALLOWED', message: '仅支持 GET。' })
          return
        }
        try {
          sendJson(response, 200, bridge.credential())
        } catch (error) {
          sendJson(response, 503, {
            code: 'TALARIA_BOOTSTRAP_UNAVAILABLE',
            message: error.message,
          })
        }
      },
    }),
  )

  ctx.effect(() =>
    ctx.webServer.register({
      kind: 'exact',
      path: BRIEFING_PATH,
      handler(request, response) {
        handleBriefing(request, response).catch((error) => {
          if (!response.headersSent) {
            sendJson(response, 500, { code: 'TALARIA_BRIEFING_ERROR', message: error.message })
          }
        })
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

  if (config.desktop && config.credentialFile) {
    ctx.effect(() => {
      try {
        return advertiseDesktop(ctx, config.credentialFile)
      } catch (error) {
        console.warn(`[talaria] 无法发布桌面协同宿主地址：${error.message}`)
      }
    })
  }
  if (config.service) {
    console.warn('[talaria] @talaria/dsh-plugin 不再托管 Resident Service 启停生命周期，请独立运行 talaria-data up。')
  }
}
