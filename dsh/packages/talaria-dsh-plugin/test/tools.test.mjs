import assert from 'node:assert/strict'
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import { createServer } from 'node:http'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import test from 'node:test'

import { apply, inject, name, TOOL_NAMES } from '../tools.js'

function listen(server) {
  return new Promise((resolve, reject) => {
    server.once('error', reject)
    server.listen(0, '127.0.0.1', () => {
      resolve(`http://127.0.0.1:${server.address().port}`)
    })
  })
}

test('exports preset-scoped plugin metadata', () => {
  assert.equal(name, '@talaria/dsh-plugin/tools')
  assert.deepEqual(inject, ['tools'])
  assert.deepEqual(TOOL_NAMES, [
    'get_snapshot',
    'get_indicators',
    'get_key_levels',
    'compute_operator',
    'get_chart_view',
    'write_agent_overlay',
    'create_alert',
  ])
})

test('registers the seven Talaria tools in preset scope and disposes on teardown', () => {
  const registered = []
  const disposedTools = []
  const effectCleanups = []
  const ctx = {
    tools: {
      register(tool) {
        registered.push(tool)
        return () => disposedTools.push(tool.name)
      },
    },
    effect(fn) {
      effectCleanups.push(fn())
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
  assert.match(byName.get_snapshot.description, /只读；返回的 snapshot_id 必须用于后续所有读取。$/)
  for (const readOnlyName of ['get_indicators', 'get_key_levels', 'compute_operator', 'get_chart_view']) {
    assert.match(byName[readOnlyName].description, /只读；snapshot_id 必须来自 get_snapshot。$/)
  }
  for (const writeName of ['write_agent_overlay', 'create_alert']) {
    assert.doesNotMatch(byName[writeName].description, /只读/)
    assert.match(byName[writeName].description, /snapshot_id 必须来自 get_snapshot。$/)
  }
  assert.equal('draft' in byName.create_alert.parameters.properties, false)
  assert.equal('status' in byName.write_agent_overlay.parameters.properties, false)
  assert.equal('status' in byName.create_alert.parameters.properties, false)

  assert.equal(effectCleanups.length, 7)
  assert.deepEqual(disposedTools, [])
  effectCleanups.reverse().forEach((dispose) => dispose?.())
  assert.equal(disposedTools.length, 7)
})

test('registered tools call the local HTTP bridge using credentialFile', async (t) => {
  const requests = []
  const meta = {
    snapshot_id: 'snap-tools-1',
    instrument_id: 'BTC-USDT-PERP',
    timeframes: ['1m'],
    cutoff_ts: 1700000000000,
    created_ts: 1700000001000,
    mode: 'HISTORICAL_PIT',
    bar_counts: { '1m': 100 },
  }
  const server = createServer((req, res) => {
    let body = ''
    req.on('data', (chunk) => { body += chunk })
    req.on('end', () => {
      requests.push({ method: req.method, url: req.url, auth: req.headers.authorization, body })
      res.writeHead(200, { 'content-type': 'application/json' })
      res.end(JSON.stringify(meta))
    })
  })
  const baseUrl = await listen(server)
  t.after(() => new Promise((resolve) => server.close(resolve)))

  const dir = mkdtempSync(join(tmpdir(), 'talaria-tools-'))
  t.after(() => rmSync(dir, { recursive: true, force: true }))
  const credentialFile = join(dir, 'dsh-bridge.json')
  writeFileSync(credentialFile, JSON.stringify({ base_url: baseUrl, token: 'test-token' }))

  const registered = new Map()
  apply({
    tools: {
      register(tool) {
        registered.set(tool.name, tool)
      },
    },
  }, { credentialFile })

  const snapshotTool = registered.get('get_snapshot')
  const result = await snapshotTool.execute(
    { instrument_id: 'BTC-USDT-PERP', timeframes: ['1m'] },
    { signal: undefined },
  )
  assert.equal(result.snapshot_id, 'snap-tools-1')
  assert.equal(requests.length, 1)
  assert.equal(requests[0].method, 'POST')
  assert.equal(requests[0].url, '/api/snapshots')
  assert.equal(requests[0].auth, 'Bearer test-token')
})
