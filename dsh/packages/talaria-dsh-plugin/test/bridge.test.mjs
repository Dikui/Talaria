import assert from 'node:assert/strict'
import { createServer } from 'node:http'
import { mkdtempSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import test from 'node:test'

import { BridgeError, createBridge, renderSnapshot, SNAPSHOT_OUTPUT_SCHEMA } from '../bridge.js'

function listen(server) {
  return new Promise((resolve) => server.listen(0, '127.0.0.1', () => resolve(server.address().port)))
}

const META = {
  snapshot_id: 'snap-abc',
  instrument_id: 'BTC-USDT-PERP',
  timeframes: ['1m'],
  cutoff_ts: 1700000000000,
  created_ts: 1700000001000,
  mode: 'HISTORICAL_PIT',
  bar_counts: { '1m': 300 },
}

test('POST /api/snapshots with Bearer token and return bound snapshot_id', async () => {
  const requests = []
  const server = createServer((req, res) => {
    let body = ''
    req.on('data', (chunk) => (body += chunk))
    req.on('end', () => {
      requests.push({ url: req.url, method: req.method, auth: req.headers.authorization, body })
      res.setHeader('content-type', 'application/json')
      res.end(JSON.stringify(META))
    })
  })
  const port = await listen(server)
  try {
    const bridge = createBridge({ baseUrl: `http://127.0.0.1:${port}`, token: 'secret-token' })
    const value = await bridge.createSnapshot({
      instrument_id: 'BTC-USDT-PERP',
      timeframes: ['1m'],
      mode: 'HISTORICAL_PIT',
    })
    assert.equal(value.snapshot_id, 'snap-abc')
    assert.equal(requests.length, 1)
    assert.equal(requests[0].method, 'POST')
    assert.equal(requests[0].url, '/api/snapshots')
    assert.equal(requests[0].auth, 'Bearer secret-token')
    assert.deepEqual(JSON.parse(requests[0].body), {
      instrument_id: 'BTC-USDT-PERP',
      timeframes: ['1m'],
      mode: 'HISTORICAL_PIT',
    })
  } finally {
    server.close()
  }
})

test('credential file is the default bridge source', async () => {
  const dir = mkdtempSync(join(tmpdir(), 'talaria-bridge-'))
  const file = join(dir, 'dsh-bridge.json')
  writeFileSync(file, JSON.stringify({ base_url: 'http://127.0.0.1:9', token: 'file-token' }))
  const server = createServer((req, res) => {
    assert.equal(req.headers.authorization, 'Bearer file-token')
    res.end(JSON.stringify(META))
  })
  const port = await listen(server)
  writeFileSync(file, JSON.stringify({ base_url: `http://127.0.0.1:${port}`, token: 'file-token' }))
  try {
    const value = await createBridge({ credentialFile: file }).createSnapshot({
      instrument_id: 'BTC-USDT-PERP',
      timeframes: ['1m'],
    })
    assert.equal(value.snapshot_id, 'snap-abc')
  } finally {
    server.close()
  }
})

test('missing credential explains talaria up', async () => {
  const bridge = createBridge({ credentialFile: join(tmpdir(), 'does-not-exist.json'), cwd: join(tmpdir(), 'missing-cwd') })
  await assert.rejects(
    () => bridge.createSnapshot({ instrument_id: 'BTC-USDT-PERP', timeframes: ['1m'] }),
    (error) => {
      assert.ok(error instanceof BridgeError)
      assert.equal(error.code, 'TALARIA_BRIDGE_UNAVAILABLE')
      assert.match(error.message, /talaria up/)
      return true
    },
  )
})

test('non-loopback base_url is rejected', async () => {
  const bridge = createBridge({ baseUrl: 'http://example.com:8765', token: 'x' })
  await assert.rejects(
    () => bridge.createSnapshot({ instrument_id: 'BTC-USDT-PERP', timeframes: ['1m'] }),
    (error) => {
      assert.equal(error.code, 'TALARIA_BRIDGE_CONFIG')
      assert.match(error.message, /回环/)
      return true
    },
  )
})

test('HTTP error and missing snapshot_id fail closed', async () => {
  const server = createServer((req, res) => {
    res.statusCode = 401
    res.setHeader('content-type', 'application/json')
    res.end(JSON.stringify({ code: 'UNAUTHORIZED', message: 'bad token' }))
  })
  const port = await listen(server)
  try {
    const bridge = createBridge({ baseUrl: `http://127.0.0.1:${port}`, token: 'x' })
    await assert.rejects(
      () => bridge.createSnapshot({ instrument_id: 'BTC-USDT-PERP', timeframes: ['1m'] }),
      (error) => {
        assert.equal(error.code, 'TALARIA_BRIDGE_HTTP')
        assert.match(error.message, /bad token/)
        return true
      },
    )
  } finally {
    server.close()
  }

  const empty = createServer((req, res) => {
    res.end(JSON.stringify({ instrument_id: 'BTC-USDT-PERP' }))
  })
  const emptyPort = await listen(empty)
  try {
    const bridge = createBridge({ baseUrl: `http://127.0.0.1:${emptyPort}`, token: 'x' })
    await assert.rejects(
      () => bridge.createSnapshot({ instrument_id: 'BTC-USDT-PERP', timeframes: ['1m'] }),
      (error) => error.code === 'TALARIA_BRIDGE_PROTOCOL',
    )
  } finally {
    empty.close()
  }
})

test('tool output schema and render bind the snapshot id', () => {
  assert.equal(SNAPSHOT_OUTPUT_SCHEMA.required.includes('snapshot_id'), true)
  const blocks = renderSnapshot({}, META)
  assert.equal(blocks.length, 1)
  assert.match(blocks[0].text, /snap-abc/)
  assert.match(blocks[0].text, /1m:300/)
})
