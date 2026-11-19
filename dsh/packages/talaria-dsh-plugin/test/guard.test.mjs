import assert from 'node:assert/strict'
import test from 'node:test'

import { callFingerprint, createDeskGuard, inheritedWriteFingerprints } from '../guard.js'

function fakeSession(events, { seeded = true } = {}) {
  return {
    header: { isSeeded: seeded },
    inheritedEventCount: events.length,
    eventAt(seq) {
      return events[seq]
    },
  }
}

const history = fakeSession([
  {
    type: 'tool/call',
    seq: 0,
    data: { name: 'place_order', arguments: '{"symbol":"BTC-USDT-PERP","qty":"0.1"}' },
  },
  {
    type: 'tool/call',
    seq: 1,
    data: { name: 'get_snapshot', arguments: '{"instrument_id":"BTC-USDT-PERP"}' },
  },
])

test('allowlisted read-only tools pass', () => {
  const guard = createDeskGuard({ getSession: () => history })
  assert.equal(guard({ name: 'get_snapshot', arguments: {} }), undefined)
  assert.equal(guard({ name: 'read', arguments: { path: 'a' } }), undefined)
  for (const name of ['get_indicators', 'get_key_levels', 'compute_operator', 'get_chart_view']) {
    assert.equal(guard({ name, arguments: { snapshot_id: 'snap-abc' } }), undefined)
  }
})

test('fork history write replay is denied with a monotonic reason', () => {
  const guard = createDeskGuard({ getSession: () => history })
  const reason = guard({
    name: 'place_order',
    arguments: { symbol: 'BTC-USDT-PERP', qty: '0.1' },
  })
  assert.match(reason, /单调守卫/)
  assert.match(reason, /place_order/)
})

test('write with changed arguments is still denied by the read-only boundary', () => {
  const guard = createDeskGuard({ getSession: () => history })
  const reason = guard({
    name: 'place_order',
    arguments: { symbol: 'BTC-USDT-PERP', qty: '0.2' },
  })
  assert.match(reason, /权限边界/)
})

test('non-seeded session write is denied but read is allowed', () => {
  const guard = createDeskGuard({ getSession: () => fakeSession([], { seeded: false }) })
  assert.match(guard({ name: 'place_order', arguments: {} }), /权限边界/)
  assert.equal(guard({ name: 'get_snapshot', arguments: {} }), undefined)
})

test('missing session cannot escape the read-only boundary', () => {
  const guard = createDeskGuard({ getSession: () => undefined })
  assert.match(guard({ name: 'place_order', arguments: {} }), /权限边界/)
})

test('inherited fingerprints ignore read-only history and normalize key order', () => {
  const prints = inheritedWriteFingerprints(history)
  assert.equal(prints.has(callFingerprint('place_order', { qty: '0.1', symbol: 'BTC-USDT-PERP' })), true)
  assert.equal(prints.has(callFingerprint('get_snapshot', { instrument_id: 'BTC-USDT-PERP' })), false)
})

test('controlled writes pass the boundary but are still guarded against fork replay', () => {
  const seeded = fakeSession([
    {
      type: 'tool/call',
      seq: 0,
      data: {
        name: 'write_agent_overlay',
        arguments: '{"snapshot_id":"snap-abc","instrument_id":"BTC-USDT-PERP"}',
      },
    },
  ])
  const guard = createDeskGuard({ getSession: () => seeded })
  assert.match(
    guard({
      name: 'write_agent_overlay',
      arguments: { snapshot_id: 'snap-abc', instrument_id: 'BTC-USDT-PERP' },
    }),
    /单调守卫/,
  )
  assert.equal(
    guard({
      name: 'write_agent_overlay',
      arguments: { snapshot_id: 'snap-abc', instrument_id: 'ETH-USDT-PERP' },
    }),
    undefined,
  )
  assert.equal(guard({ name: 'create_alert', arguments: { snapshot_id: 'snap-abc' } }), undefined)
})

test('controlled writes pass in a fresh session but other writes still fail', () => {
  const guard = createDeskGuard({ getSession: () => fakeSession([], { seeded: false }) })
  assert.equal(guard({ name: 'write_agent_overlay', arguments: {} }), undefined)
  assert.equal(guard({ name: 'create_alert', arguments: {} }), undefined)
  assert.match(guard({ name: 'place_order', arguments: {} }), /权限边界/)
  assert.match(guard({ name: 'delete_drawing', arguments: {} }), /权限边界/)
})
