import assert from 'node:assert/strict'
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import { createServer } from 'node:http'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { Readable } from 'node:stream'
import test from 'node:test'

import { apply } from '../index.js'

function listen(server) {
  return new Promise((resolve, reject) => {
    server.once('error', reject)
    server.listen(0, '127.0.0.1', () => {
      resolve(`http://127.0.0.1:${server.address().port}`)
    })
  })
}

function fakeResponse() {
  const chunks = []
  return {
    headersSent: false,
    status: 0,
    writeHead(status) {
      this.status = status
      this.headersSent = true
    },
    end(payload) {
      chunks.push(String(payload ?? ''))
      this.body = chunks.join('')
    },
    json() {
      return JSON.parse(this.body)
    },
  }
}

function fakeRequest(body) {
  const request = Readable.from([Buffer.from(JSON.stringify(body))])
  request.method = 'POST'
  return request
}

function fakeAgent() {
  const events = []
  const session = {
    id: 'dsh-session-consult',
    get seq() {
      return events.length
    },
    eventAt(seq) {
      return events[seq]
    },
  }
  const agent = {
    session,
    followup() {
      events.push({ type: 'turn/start', data: { turn: 1 } })
      events.push({
        type: 'assistant/message',
        data: {
          turn: 1,
          step: 1,
          message: { content: [{ type: 'text', text: '观察建议：等待回踩承接。' }] },
          usage: { inputTokens: 100, cacheReadTokens: 40, outputTokens: 25 },
        },
      })
      events.push({ type: 'turn/end', data: { turn: 1 } })
      return Promise.resolve()
    },
    whenIdle() {
      return Promise.resolve()
    },
    cancel() {},
  }
  return agent
}

async function waitFor(check, timeoutMs = 2000) {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    if (check()) return
    await new Promise((resolve) => setTimeout(resolve, 20))
  }
  throw new Error('等待超时')
}

test('registers five read-only tools plus the two controlled writes and the desk guard', () => {
  const registered = []
  let guarded = 0
  const ctx = {
    tools: {
      register(tool) {
        registered.push(tool)
      },
      guard() {
        guarded += 1
      },
    },
    sessions: { get: () => undefined },
    webServer: { register: () => () => {} },
    effect(fn) {
      fn()
    },
    get() {
      return undefined
    },
  }
  apply(ctx, {})
  assert.deepEqual(
    registered.map((tool) => tool.name).sort(),
    [
      'compute_operator',
      'create_alert',
      'get_chart_view',
      'get_indicators',
      'get_key_levels',
      'get_snapshot',
      'write_agent_overlay',
    ],
  )
  for (const tool of registered) {
    assert.equal(tool.parameters.type, 'object')
    assert.equal(tool.parameters.additionalProperties, false)
    if (tool.name !== 'get_snapshot') {
      assert.equal(tool.parameters.required[0], 'snapshot_id')
    }
  }
  const byName = Object.fromEntries(registered.map((tool) => [tool.name, tool]))
  assert.equal('draft' in byName.create_alert.parameters.properties, false)
  assert.equal('status' in byName.write_agent_overlay.parameters.properties, false)
  assert.equal('status' in byName.create_alert.parameters.properties, false)
  assert.equal(guarded, 1)
})

test('consult endpoint runs one agent turn and reports text plus turn tokens', async () => {
  const results = []
  const usages = []
  const server = createServer(async (request, response) => {
    const chunks = []
    for await (const chunk of request) chunks.push(chunk)
    if (request.url.endsWith('/result')) {
      results.push(JSON.parse(Buffer.concat(chunks).toString('utf8')))
      response.writeHead(200, { 'content-type': 'application/json' })
      response.end(JSON.stringify({ ok: true }))
      return
    }
    if (request.url === '/api/collaboration/usage') {
      usages.push(JSON.parse(Buffer.concat(chunks).toString('utf8')))
      response.writeHead(200, { 'content-type': 'application/json' })
      response.end(JSON.stringify({ recorded: true, duplicate: false }))
      return
    }
    response.writeHead(404)
    response.end()
  })
  const baseUrl = await listen(server)
  const dir = mkdtempSync(join(tmpdir(), 'talaria-plugin-'))
  const descriptor = join(dir, 'dsh-bridge.json')
  writeFileSync(
    descriptor,
    JSON.stringify({ base_url: baseUrl, token: 'test-token' }),
  )
  process.env.TALARIA_DSH_BRIDGE = descriptor

  const endpoints = new Map()
  const registered = []
  const ctx = {
    tools: {
      register(tool) {
        registered.push(tool)
      },
      guard() {},
    },
    sessions: { get: () => undefined },
    webServer: {
      register({ path, handler }) {
        endpoints.set(path, handler)
        return () => {}
      },
    },
    effect(fn) {
      fn()
    },
    get(name) {
      if (name === 'agents') {
        return {
          create: async () => ({ agent: fakeAgent(), dispose: async () => {} }),
        }
      }
      if (name === 'agentDefaultModel') {
        return {
          currentSelection: () => ({ provider: 'test', model: 'test-model' }),
        }
      }
      return undefined
    },
  }

  try {
    apply(ctx, {})
    const consult = endpoints.get('/talaria/consult')
    assert.equal(typeof consult, 'function')
    const response = fakeResponse()
    await consult(
      fakeRequest({ consultation_id: 'cons_1', prompt: '现在适合入场吗？' }),
      response,
    )
    await waitFor(() => response.headersSent)
    assert.equal(response.status, 202)
    assert.deepEqual(response.json(), { accepted: true, consultation_id: 'cons_1' })

    await waitFor(() => results.length === 1 && usages.length === 1)
    assert.equal(results[0].text, '观察建议：等待回踩承接。')
    assert.equal(results[0].dsh_session_id, 'dsh-session-consult')
    assert.equal(results[0].dsh_turn_id, '1')
    assert.deepEqual(usages[0], {
      dsh_session_id: 'dsh-session-consult',
      dsh_turn_id: '1',
      input_tokens: 100,
      cached_input_tokens: 40,
      output_tokens: 25,
      source: 'host',
    })
  } finally {
    delete process.env.TALARIA_DSH_BRIDGE
    rmSync(dir, { recursive: true, force: true })
    await new Promise((resolve) => server.close(resolve))
  }
})

test('consult endpoint rejects duplicate and malformed requests', async () => {
  const endpoints = new Map()
  const ctx = {
    tools: { register() {}, guard() {} },
    sessions: { get: () => undefined },
    webServer: {
      register({ path, handler }) {
        endpoints.set(path, handler)
        return () => {}
      },
    },
    effect(fn) {
      fn()
    },
    get: () => undefined,
  }
  apply(ctx, {})
  const consult = endpoints.get('/talaria/consult')

  const malformed = fakeResponse()
  await consult(fakeRequest({ prompt: '缺少会诊 id' }), malformed)
  await waitFor(() => malformed.headersSent)
  assert.equal(malformed.status, 400)
  assert.equal(malformed.json().code, 'INVALID_REQUEST')

  const noPrompt = fakeResponse()
  await consult(fakeRequest({ consultation_id: 'cons_x' }), noPrompt)
  await waitFor(() => noPrompt.headersSent)
  assert.equal(noPrompt.status, 400)
})

test('briefing endpoint runs one agent turn and submits text with turn usage', async () => {
  const results = []
  const server = createServer(async (request, response) => {
    const chunks = []
    for await (const chunk of request) chunks.push(chunk)
    if (request.url === '/api/scanner/briefings/brf_1/result') {
      results.push(JSON.parse(Buffer.concat(chunks).toString('utf8')))
      response.writeHead(200, { 'content-type': 'application/json' })
      response.end(JSON.stringify({ ok: true }))
      return
    }
    response.writeHead(404)
    response.end()
  })
  const baseUrl = await listen(server)
  const dir = mkdtempSync(join(tmpdir(), 'talaria-plugin-'))
  const descriptor = join(dir, 'dsh-bridge.json')
  writeFileSync(
    descriptor,
    JSON.stringify({ base_url: baseUrl, token: 'test-token' }),
  )
  process.env.TALARIA_DSH_BRIDGE = descriptor

  const endpoints = new Map()
  const ctx = {
    tools: { register() {}, guard() {} },
    sessions: { get: () => undefined },
    webServer: {
      register({ path, handler }) {
        endpoints.set(path, handler)
        return () => {}
      },
    },
    effect(fn) {
      fn()
    },
    get(name) {
      if (name === 'agents') {
        return {
          create: async () => ({ agent: fakeAgent(), dispose: async () => {} }),
        }
      }
      if (name === 'agentDefaultModel') {
        return {
          currentSelection: () => ({ provider: 'test', model: 'test-model' }),
        }
      }
      return undefined
    },
  }

  try {
    apply(ctx, {})
    const briefing = endpoints.get('/talaria/briefing')
    assert.equal(typeof briefing, 'function')
    const response = fakeResponse()
    await briefing(
      fakeRequest({ briefing_id: 'brf_1', prompt: '解读这次放量。' }),
      response,
    )
    await waitFor(() => response.headersSent)
    assert.equal(response.status, 202)
    assert.deepEqual(response.json(), { accepted: true, briefing_id: 'brf_1' })

    await waitFor(() => results.length === 1)
    assert.equal(results[0].text, '观察建议：等待回踩承接。')
    assert.equal(results[0].dsh_session_id, 'dsh-session-consult')
    assert.equal(results[0].dsh_turn_id, '1')
    assert.deepEqual(results[0].usage, {
      dsh_session_id: 'dsh-session-consult',
      dsh_turn_id: '1',
      input_tokens: 100,
      cached_input_tokens: 40,
      output_tokens: 25,
      source: 'host',
    })
  } finally {
    delete process.env.TALARIA_DSH_BRIDGE
    rmSync(dir, { recursive: true, force: true })
    await new Promise((resolve) => server.close(resolve))
  }
})
