import assert from 'node:assert/strict'
import { existsSync, readFileSync } from 'node:fs'
import { test } from 'node:test'

import { apply } from '../index.js'

test('service-supervisor.js is retired and apply never spawns Resident Service', () => {
  assert.equal(existsSync(new URL('../service-supervisor.js', import.meta.url)), false)
  const indexSource = readFileSync(new URL('../index.js', import.meta.url), 'utf8')
  assert.doesNotMatch(indexSource, /service-supervisor|superviseService|spawn/)

  const effects = []
  const warnings = []
  const originalWarn = console.warn
  console.warn = (...args) => warnings.push(args.join(' '))
  try {
    apply(
      {
        tools: { register() {}, guard() {} },
        sessions: { get: () => undefined },
        webServer: { register: () => () => {} },
        effect(fn) {
          effects.push(fn())
        },
      },
      {
        service: {
          command: ['/nonexistent/talaria', 'up', '--solo'],
          cwd: '/tmp',
        },
      },
    )
  } finally {
    console.warn = originalWarn
  }

  assert.equal(warnings.length, 1)
  assert.match(warnings[0], /talaria-data/)
  assert.equal(effects.length, 3)
})
