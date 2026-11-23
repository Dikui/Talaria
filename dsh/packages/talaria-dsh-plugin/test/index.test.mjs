import assert from 'node:assert/strict'
import { mkdtempSync, readFileSync, rmSync, statSync, writeFileSync } from 'node:fs'
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
  request.headers = { 'content-type': 'application/json' }
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

test('does not register tools on root ctx and registers only the desk guard', () => {
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
  assert.equal(registered.length, 0)
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
  const createCalls = []
  const mountedPresets = []
  const ctx = {
    tools: {
      register(tool) {
        registered.push(tool)
      },
      guard() {},
    },
    sessions: { get: () => undefined },
    agentPresets: {
      async mount(agentCtx, preset) {
        mountedPresets.push({ agentCtx, preset })
      },
    },
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
          create: async (options) => {
            createCalls.push(options)
            await options.setup?.({ id: 'mock-consult-ctx' })
            return { agent: fakeAgent(), dispose: async () => {} }
          },
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
    assert.equal(registered.length, 0)
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
    assert.equal(createCalls.length, 1)
    assert.deepEqual(createCalls[0].meta, { agentPreset: 'talaria-desk' })
    assert.deepEqual(mountedPresets, [{ agentCtx: { id: 'mock-consult-ctx' }, preset: 'talaria-desk' }])
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
  const createCalls = []
  const mountedPresets = []
  const ctx = {
    tools: { register() {}, guard() {} },
    sessions: { get: () => undefined },
    agentPresets: {
      async mount(agentCtx, preset) {
        mountedPresets.push({ agentCtx, preset })
      },
    },
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
          create: async (options) => {
            createCalls.push(options)
            await options.setup?.({ id: 'mock-briefing-ctx' })
            return { agent: fakeAgent(), dispose: async () => {} }
          },
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
    assert.equal(createCalls.length, 1)
    assert.deepEqual(createCalls[0].meta, { agentPreset: 'talaria-desk' })
    assert.deepEqual(mountedPresets, [{ agentCtx: { id: 'mock-briefing-ctx' }, preset: 'talaria-desk' }])
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

// platform/04: rc.5 reads the current preset from the session projection.
test('only Talaria consultations, briefings and preset sessions have a read-only guard', () => {
  let guard
  const ctx = {
    tools: { register() {}, guard(check) { guard = check } },
    sessions: { get: () => undefined },
    agentPresets: { presetForSession: (session) => session.selectedPreset },
    webServer: { register: () => () => {} },
    effect(fn) { fn() },
  }
  apply(ctx)
  for (const session of [
    { id: 'talaria-consult-one' },
    { id: 'talaria-briefing-one' },
    { id: 'normal', selectedPreset: 'talaria-desk' },
  ]) {
    assert.match(guard({ agent: { session }, name: 'write' }), /权限边界/)
    assert.equal(guard({ agent: { session }, name: 'get_snapshot' }), undefined)
  }
  const session = { id: 'normal', selectedPreset: 'code' }
  assert.equal(guard({ agent: { session }, name: 'write' }), undefined)
  assert.equal(guard({ name: 'write' }), undefined)
})

test('bootstrap reloads the installed credential path and reports missing credentials', (t) => {
  const directory = mkdtempSync(join(tmpdir(), 'talaria-desktop-'))
  t.after(() => rmSync(directory, { recursive: true, force: true }))
  const credentialFile = join(directory, 'dsh-bridge.json')
  const routes = new Map()
  let rejection
  apply({
    tools: { register() {}, guard() {} },
    sessions: { get: () => undefined },
    webServer: { register(route) { routes.set(route.path, route); return () => {} } },
    connection: { requestRejection: () => rejection },
    effect(fn) { fn() },
  }, { credentialFile })
  const bootstrap = routes.get('/talaria/bootstrap').handler
  const read = () => {
    const response = fakeResponse()
    bootstrap({ method: 'GET' }, response)
    return response
  }
  const credentials = { base_url: 'http://127.0.0.1:8765', token: 'first' }
  assert.equal(read().status, 503)
  writeFileSync(credentialFile, JSON.stringify(credentials))
  assert.deepEqual(read().json(), credentials)
  writeFileSync(credentialFile, JSON.stringify({ ...credentials, token: 'other' }))
  assert.equal(read().json().token, 'other')
  rejection = 401
  assert.equal(read().status, 401)
  assert.equal(read().json().token, undefined)
  rejection = undefined
  rmSync(credentialFile)
  assert.equal(read().status, 503)
})

test('desktop plugin publishes the bound collaboration origin even before Talaria starts', (t) => {
  const directory = mkdtempSync(join(tmpdir(), 'talaria-desktop-'))
  t.after(() => rmSync(directory, { recursive: true, force: true }))
  const credentialFile = join(directory, 'runtime', 'dsh-bridge.json')
  const descriptor = join(directory, 'runtime', 'dsh-desktop.json')
  const disposers = []
  const origin = 'http://127.0.0.1:32190'
  apply({
    tools: { register() {}, guard() {} },
    sessions: { get: () => undefined },
    webServer: { getOrigin: () => origin, register: () => () => {} },
    effect(fn) { disposers.push(fn()) },
  }, { credentialFile, desktop: true })
  const payload = JSON.parse(readFileSync(descriptor, 'utf8'))
  assert.equal(payload.base_url, origin)
  assert.equal(payload.pid, process.pid)
  assert.equal(statSync(descriptor).mode & 0o777, 0o600)
  disposers.reverse().forEach(dispose => dispose?.())
  assert.throws(() => readFileSync(descriptor), { code: 'ENOENT' })
})
