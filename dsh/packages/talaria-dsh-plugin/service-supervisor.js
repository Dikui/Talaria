import { spawn } from 'node:child_process'
import { createHash } from 'node:crypto'
import { closeSync, mkdirSync, openSync } from 'node:fs'
import { readFile, realpath } from 'node:fs/promises'
import { get } from 'node:http'
import { join } from 'node:path'

import { assertLoopback } from './bridge.js'

const EXTERNAL_POLL_MS = 5_000
const STOP_TIMEOUT_MS = 10_000
const INITIAL_RESTART_MS = 1_000
const MAX_RESTART_MS = 60_000
const STABLE_RUN_MS = 60_000
const PROXY_NAMES = new Set(['http_proxy', 'https_proxy', 'all_proxy', 'no_proxy'])

// DSH 会把系统代理解析结果（含 httpx 无法解析的 `[::1]`）写进自身环境；
// 服务只认安装时记录的代理配置，丢弃继承来的任意大小写代理变量。
export function serviceEnv(inherited, configured = {}) {
  const env = {}
  for (const [name, value] of Object.entries(inherited)) {
    if (!PROXY_NAMES.has(name.toLowerCase())) env[name] = value
  }
  return { ...env, ...configured }
}

function checkHealth(url, dataDirId, request) {
  return new Promise((resolve, reject) => {
    const pending = request(url, { agent: false, signal: AbortSignal.timeout(1_000) }, (response) => {
      const dataDir = response.headers['x-talaria-data-dir']
      resolve(response.statusCode === 200 && dataDir === dataDirId)
      response.destroy()
    })
    pending.once('error', reject)
  })
}

export async function probeExternalService(cwd, request = get) {
  const runtime = join(cwd, 'runtime')
  try {
    // Node 只读锁记录；Python 服务启动时仍会取得实际的 flock 排他锁。
    const lock = JSON.parse(await readFile(join(runtime, 'service.lock'), 'utf8'))
    const descriptor = JSON.parse(await readFile(join(runtime, 'service-descriptor.json'), 'utf8'))
    if (lock?.role !== 'service' || !Number.isSafeInteger(lock.pid) || lock.pid <= 0
      || descriptor?.version !== 1 || descriptor.pid !== lock.pid) return false
    process.kill(lock.pid, 0)
    const url = new URL('/api/health', assertLoopback(descriptor.base_url))
    const dataDirId = createHash('sha256').update(await realpath(cwd, { encoding: 'buffer' })).digest('hex')
    return await checkHealth(url, dataDirId, request)
  } catch {
    return false
  }
}

export function superviseService({ command, cwd, env, logDir }, {
  spawnChild = spawn,
  probe = probeExternalService,
  schedule = setTimeout,
  cancel = clearTimeout,
  now = () => performance.now(),
} = {}) {
  let timer
  let running
  let disposed = false
  let stopping
  let restartMs = INITIAL_RESTART_MS

  function checkAfter(delay) {
    timer = schedule(() => {
      timer = undefined
      void check()
    }, delay)
  }

  function retry() {
    if (disposed) return
    checkAfter(restartMs)
    restartMs = Math.min(restartMs * 2, MAX_RESTART_MS)
  }

  function startChild() {
    const descriptors = []
    try {
      mkdirSync(logDir, { recursive: true, mode: 0o700 })
      for (const name of ['service.out.log', 'service.err.log']) {
        descriptors.push(openSync(join(logDir, name), 'a', 0o600))
      }
      return spawnChild(command[0], command.slice(1), {
        cwd, env: serviceEnv(process.env, env), stdio: ['pipe', ...descriptors],
      })
    } finally {
      for (const fd of descriptors) closeSync(fd)
    }
  }

  async function check() {
    try {
      const external = await probe(cwd)
      if (disposed) return
      if (external) {
        checkAfter(EXTERNAL_POLL_MS)
        return
      }

      const child = startChild()
      const startedAt = now()
      // 子进程可能在关闭标准输入前就已经退出。
      child.stdin?.on('error', () => {})
      child.on('error', (error) => {
        console.warn(`[talaria] 常驻服务子进程出错：${error.message}`)
      })
      const finished = new Promise((resolve) => {
        child.once('close', () => {
          running = undefined
          if (now() - startedAt >= STABLE_RUN_MS) restartMs = INITIAL_RESTART_MS
          retry()
          resolve()
        })
      })
      running = { child, finished }
    } catch (error) {
      console.warn(`[talaria] 无法启动常驻服务：${error.message}`)
      retry()
    }
  }

  async function stop() {
    disposed = true
    if (timer !== undefined) cancel(timer)
    if (!running) return

    const { child, finished } = running
    child.stdin?.end()
    child.kill('SIGTERM')
    const deadline = schedule(() => child.kill('SIGKILL'), STOP_TIMEOUT_MS)
    try {
      await finished
    } finally {
      cancel(deadline)
    }
  }

  void check()
  return () => (stopping ??= stop())
}
