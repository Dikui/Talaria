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

test('read-only tools bind the frozen snapshot and keep decimal strings', async () => {
  const requests = []
  const server = createServer((req, res) => {
    let body = ''
    req.on('data', (chunk) => (body += chunk))
    req.on('end', () => {
      requests.push({ url: req.url, method: req.method, body })
      res.setHeader('content-type', 'application/json')
      if (req.url === '/api/snapshots/snap-abc') {
        res.end(JSON.stringify(META))
        return
      }
      if (req.url.startsWith('/api/snapshots/snap-abc/indicators')) {
        res.end(JSON.stringify({
          snapshot_id: 'snap-abc',
          instrument_id: 'BTC-USDT-PERP',
          timeframe: '1m',
          series: [{ name: 'EMA_20', points: [{ timestamp: 1700000000000, values: { EMA_20: '60000.5' } }] }],
        }))
        return
      }
      if (req.url.startsWith('/api/snapshots/snap-abc/key-levels')) {
        res.end(JSON.stringify({
          snapshot_id: 'snap-abc',
          instrument_id: 'BTC-USDT-PERP',
          timeframe: '1m',
          levels: [{ level_id: 'kl-1', kind: 'SWING_HIGH', price: '60100.25' }],
        }))
        return
      }
      if (req.url === '/api/operators/compute') {
        res.end(JSON.stringify({ name: 'ATR', points: [{ timestamp: 1700000000000, values: { ATR: '12.5' } }] }))
        return
      }
      if (req.url.startsWith('/api/chart-view')) {
        res.end(JSON.stringify({
          instrument: { instrument_id: 'BTC-USDT-PERP' },
          timeframe: '1m',
          window: { from_ts: 1, to_ts: 2, price_min: '59000', price_max: '61000' },
          drawings: [],
        }))
        return
      }
      res.statusCode = 404
      res.end(JSON.stringify({ code: 'NOT_FOUND', message: req.url }))
    })
  })
  const port = await listen(server)
  try {
    const bridge = createBridge({ baseUrl: `http://127.0.0.1:${port}`, token: 'x' })
    const indicators = await bridge.getIndicators({
      snapshot_id: 'snap-abc',
      instrument_id: 'BTC-USDT-PERP',
      timeframe: '1m',
      names: ['EMA_20', 'MACD'],
      limit: 120,
    })
    assert.equal(indicators.snapshot_id, 'snap-abc')
    assert.equal(indicators.series[0].points[0].values.EMA_20, '60000.5')

    const levels = await bridge.getKeyLevels({
      snapshot_id: 'snap-abc',
      instrument_id: 'BTC-USDT-PERP',
      timeframe: '1m',
      categories: ['STRUCTURE', 'VOLUME'],
      limit: 20,
      min_strength: '0.5',
    })
    assert.equal(levels.levels[0].price, '60100.25')

    const operator = await bridge.computeOperator({
      snapshot_id: 'snap-abc',
      instrument_id: 'BTC-USDT-PERP',
      timeframe: '1m',
      operator: 'ATR',
      params: { window: '14' },
      limit: 120,
    })
    assert.equal(operator.snapshot_id, 'snap-abc')
    assert.equal(operator.instrument_id, 'BTC-USDT-PERP')
    assert.equal(operator.series.points[0].values.ATR, '12.5')

    const view = await bridge.getChartView({
      snapshot_id: 'snap-abc',
      instrument_id: 'BTC-USDT-PERP',
      timeframe: '1m',
      from_ts: 1,
      to_ts: 2,
      price_min: '59000',
      price_max: '61000',
    })
    assert.equal(view.snapshot_id, 'snap-abc')
    assert.equal(view.window.price_min, '59000')

    const indicatorRequest = requests.find((item) => item.url.includes('/indicators'))
    assert.match(indicatorRequest.url, /timeframe=1m/)
    assert.match(indicatorRequest.url, /names=EMA_20%2CMACD/)
    assert.match(indicatorRequest.url, /limit=120/)
    const levelRequest = requests.find((item) => item.url.includes('/key-levels'))
    assert.match(levelRequest.url, /categories=STRUCTURE%2CVOLUME/)
    assert.match(levelRequest.url, /min_strength=0.5/)
    const operatorRequest = requests.find((item) => item.url === '/api/operators/compute')
    assert.equal(operatorRequest.method, 'POST')
    assert.deepEqual(JSON.parse(operatorRequest.body), {
      snapshot_id: 'snap-abc',
      timeframe: '1m',
      operator: 'ATR',
      params: { window: '14' },
      limit: 120,
    })
  } finally {
    server.close()
  }
})

test('read-only tools reject instrument or timeframe mismatch before reading', async () => {
  let reads = 0
  const server = createServer((req, res) => {
    reads += 1
    res.setHeader('content-type', 'application/json')
    res.end(JSON.stringify(META))
  })
  const port = await listen(server)
  try {
    const bridge = createBridge({ baseUrl: `http://127.0.0.1:${port}`, token: 'x' })
    await assert.rejects(
      () => bridge.getIndicators({
        snapshot_id: 'snap-abc',
        instrument_id: 'ETH-USDT-PERP',
        timeframe: '1m',
        names: ['EMA_20'],
      }),
      (error) => error.code === 'TALARIA_BRIDGE_SNAPSHOT',
    )
    await assert.rejects(
      () => bridge.getKeyLevels({
        snapshot_id: 'snap-abc',
        instrument_id: 'BTC-USDT-PERP',
        timeframe: '5m',
        categories: [],
      }),
      (error) => error.code === 'TALARIA_BRIDGE_SNAPSHOT',
    )
    assert.equal(reads, 2)
  } finally {
    server.close()
  }
})

test('write_agent_overlay binds the snapshot and posts the annotation', async () => {
  const requests = []
  const server = createServer((req, res) => {
    let body = ''
    req.on('data', (chunk) => (body += chunk))
    req.on('end', () => {
      requests.push({ url: req.url, method: req.method, body })
      res.setHeader('content-type', 'application/json')
      if (req.method === 'GET') {
        res.end(JSON.stringify(META))
        return
      }
      res.end(JSON.stringify({
        annotation_id: 'ann_1',
        snapshot_id: 'snap-abc',
        instrument_id: 'BTC-USDT-PERP',
        timeframe: '1m',
        status: 'ACTIVE',
        round: 1,
      }))
    })
  })
  const port = await listen(server)
  try {
    const bridge = createBridge({ baseUrl: `http://127.0.0.1:${port}`, token: 'token' })
    const value = await bridge.writeAgentOverlay({
      snapshot_id: 'snap-abc',
      instrument_id: 'BTC-USDT-PERP',
      timeframe: '1m',
      session_id: 'session-1',
      round: 1,
      kind: 'LEVEL',
      label: '前低',
      price_low: '100',
      price_high: '100',
      ts_anchor: 1700000000000,
      evidence_refs: [{ level_id: 'kl_1' }],
    })
    assert.equal(value.annotation_id, 'ann_1')
    const posted = requests.find((request) => request.method === 'POST')
    assert.equal(posted.url, '/api/annotations')
    const body = JSON.parse(posted.body)
    assert.equal(body.snapshot_id, 'snap-abc')
    assert.equal(body.kind, 'LEVEL')
  } finally {
    server.close()
  }
})

test('create_alert always posts draft=true and refuses a non-draft response', async () => {
  const requests = []
  let status = 'DRAFT'
  const server = createServer((req, res) => {
    let body = ''
    req.on('data', (chunk) => (body += chunk))
    req.on('end', () => {
      requests.push({ url: req.url, method: req.method, body })
      res.setHeader('content-type', 'application/json')
      if (req.method === 'GET') {
        res.end(JSON.stringify(META))
        return
      }
      res.end(JSON.stringify({
        alert_id: 'alr_1',
        instrument_id: 'BTC-USDT-PERP',
        timeframe: '1m',
        status,
        note: '草案',
      }))
    })
  })
  const rule = {
    version: '1',
    timing: 'event',
    expression: {
      op: 'compare',
      test: 'gte',
      left: { source: 'event', metric: 'last_price' },
      right: { source: 'constant', value: '100000' },
    },
  }
  const port = await listen(server)
  try {
    const bridge = createBridge({ baseUrl: `http://127.0.0.1:${port}`, token: 'token' })
    const value = await bridge.createAlertDraft({
      snapshot_id: 'snap-abc',
      instrument_id: 'BTC-USDT-PERP',
      timeframe: '1m',
      condition: rule,
    })
    assert.equal(value.status, 'DRAFT')
    const posted = JSON.parse(requests.find((request) => request.method === 'POST').body)
    assert.equal(posted.draft, true)
    assert.deepEqual(posted.condition, rule)
    assert.equal('snapshot_id' in posted, false)

    status = 'ARMED'
    await assert.rejects(
      () => bridge.createAlertDraft({
        snapshot_id: 'snap-abc',
        instrument_id: 'BTC-USDT-PERP',
        timeframe: '1m',
        condition: rule,
      }),
      (error) => {
        assert.equal(error.code, 'TALARIA_BRIDGE_PROTOCOL')
        return true
      },
    )
  } finally {
    server.close()
  }
})
