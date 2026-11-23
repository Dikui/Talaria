import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { EventEmitter } from 'node:events'
import { mkdirSync, mkdtempSync, readFileSync, realpathSync, rmSync, writeFileSync, writeSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import test from 'node:test'

import { probeExternalService, superviseService } from '../service-supervisor.js'

function tempDirectory(t) {
  const directory = mkdtempSync(join(tmpdir(), 'talaria-supervisor-'))
  t.after(() => rmSync(directory, { recursive: true, force: true }))
  return directory
}

function supervisor(t, probe = () => false) {
  const cwd = tempDirectory(t)
  const logDir = join(cwd, 'logs')
  const children = []
  const pending = new Map()
  let time = 0
  let sequence = 0

  // 用可控时钟及事件发射器替代真实等待和子进程。
  const dispose = superviseService({
    command: ['/fake/bin/talaria', 'up', '--solo'],
    cwd,
    env: { TALARIA_TEST_PROXY: 'local-proxy' },
    logDir,
  }, {
    probe,
    spawnChild(program, args, options) {
      const child = new EventEmitter()
      child.stdin = new EventEmitter()
      child.actions = []
      child.stdin.end = () => child.actions.push('EOF')
      child.kill = (signal) => child.actions.push(signal)
      children.push(child)
      child.invocation = { program, args, cwd: options.cwd, env: options.env, stdin: options.stdio[0] }
      writeSync(options.stdio[1], 'out\n')
      writeSync(options.stdio[2], 'err\n')
      return child
    },
    schedule(callback, delay) {
      const id = ++sequence
      pending.set(id, { callback, at: time + delay })
      return id
    },
    cancel(id) { pending.delete(id) },
    now: () => time,
  })

  async function advance(ms) {
    const until = time + ms
    for (const [id, { callback, at }] of pending) {
      if (at > until) continue
      pending.delete(id)
      time = at
      callback()
      await Promise.resolve()
    }
    time = until
    await Promise.resolve()
  }

  return { cwd, logDir, children, advance, dispose }
}

// platform/05: 已运行的健康服务由其原有宿主管理，退出后才接管。
test('checks every five seconds and starts only after the external service exits', async (t) => {
  let external = true
  const { cwd, children, advance, dispose } = supervisor(t, () => external)
  await advance(0)
  assert.equal(children.length, 0)
  await advance(5_000)
  assert.equal(children.length, 0)

  external = false
  await advance(4_999)
  assert.equal(children.length, 0)
  await advance(1)
  assert.deepEqual(children[0].invocation, {
    program: '/fake/bin/talaria', args: ['up', '--solo'], cwd, stdin: 'pipe',
    env: { ...process.env, TALARIA_TEST_PROXY: 'local-proxy' },
  })
  const stopped = dispose()
  children[0].emit('close', 0)
  await stopped
})

test('restarts crashes with capped exponential backoff and resets after sixty seconds', async (t) => {
  const { children, logDir, advance, dispose } = supervisor(t)
  await advance(0)
  for (const delay of [1_000, 2_000, 4_000, 8_000, 16_000, 32_000, 60_000, 60_000]) {
    const count = children.length
    children.at(-1).emit('close', 1)
    await advance(delay - 1)
    assert.equal(children.length, count)
    await advance(1)
    assert.equal(children.length, count + 1)
  }

  await advance(60_000)
  const count = children.length
  children.at(-1).emit('close', 0)
  await advance(999)
  assert.equal(children.length, count)
  await advance(1)
  assert.equal(children.length, count + 1)
  assert.equal(readFileSync(join(logDir, 'service.out.log'), 'utf8'), 'out\n'.repeat(count + 1))
  assert.equal(readFileSync(join(logDir, 'service.err.log'), 'utf8'), 'err\n'.repeat(count + 1))
  const stopped = dispose()
  children.at(-1).emit('close', 0)
  await stopped
})

test('dispose closes stdin, escalates to SIGKILL after ten seconds, and waits for the child', async (t) => {
  const { children, advance, dispose } = supervisor(t)
  await advance(0)
  const [child] = children
  let finished = false
  const stopped = dispose().then(() => { finished = true })
  assert.deepEqual(child.actions, ['EOF', 'SIGTERM'])
  await advance(9_999)
  assert.deepEqual(child.actions, ['EOF', 'SIGTERM'])
  await advance(1)
  assert.deepEqual(child.actions, ['EOF', 'SIGTERM', 'SIGKILL'])
  assert.equal(finished, false)

  child.emit('close', null, 'SIGKILL')
  await stopped
  await advance(60_000)
  assert.equal(children.length, 1)
})

test('dispose cancels a pending restart', async (t) => {
  const { children, advance, dispose } = supervisor(t)
  await advance(0)
  children[0].emit('close', 1)
  await dispose()
  await advance(60_000)
  assert.equal(children.length, 1)
})

test('dispose during a probe prevents it from starting a service', async (t) => {
  let resolveProbe
  const probe = new Promise((resolve) => { resolveProbe = resolve })
  const { children, advance, dispose } = supervisor(t, () => probe)
  await dispose()
  resolveProbe(false)
  await advance(60_000)
  assert.equal(children.length, 0)
})

test('probe requires a matching service PID, lock role and data directory on the health endpoint', async (t) => {
  const cwd = tempDirectory(t)
  const runtime = join(cwd, 'runtime')
  mkdirSync(runtime)
  const lockPath = join(runtime, 'service.lock')
  const descriptorPath = join(runtime, 'service-descriptor.json')
  const lock = { role: 'service', pid: process.pid }
  const dataDirId = createHash('sha256').update(realpathSync(cwd)).digest('hex')
  let healthDataDir = dataDirId
  let requests = 0
  // 用模拟 HTTP 响应替代 TCP，仍走健康接口的地址与响应判定。
  const request = (url, options, onResponse) => {
    requests += 1
    assert.equal(url.href, 'http://127.0.0.1:8765/api/health')
    assert.equal(options.agent, false)
    onResponse({
      statusCode: 200,
      headers: { 'x-talaria-data-dir': healthDataDir },
      destroy() {},
    })
    return new EventEmitter()
  }
  const descriptor = {
    version: 1, pid: process.pid, base_url: 'http://127.0.0.1:8765',
  }
  writeFileSync(lockPath, JSON.stringify(lock))
  writeFileSync(descriptorPath, JSON.stringify({ ...descriptor, pid: process.pid + 1 }))
  assert.equal(await probeExternalService(cwd, request), false)
  writeFileSync(lockPath, JSON.stringify({ ...lock, role: 'usage-scan' }))
  writeFileSync(descriptorPath, JSON.stringify(descriptor))
  assert.equal(await probeExternalService(cwd, request), false)
  assert.equal(requests, 0)

  writeFileSync(lockPath, JSON.stringify(lock))
  assert.equal(await probeExternalService(cwd, request), true)
  healthDataDir = 'another-directory'
  assert.equal(await probeExternalService(cwd, request), false)
})
