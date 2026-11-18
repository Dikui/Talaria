import { readFileSync, statSync } from 'node:fs'
import { homedir } from 'node:os'
import { isAbsolute, resolve } from 'node:path'

const LOOPBACK_HOSTS = new Set(['127.0.0.1', 'localhost', '::1', '[::1]'])

export class BridgeError extends Error {
  constructor(message, options = {}) {
    super(message)
    this.name = 'BridgeError'
    this.code = options.code ?? 'TALARIA_BRIDGE_ERROR'
    this.cause = options.cause
  }
}

function credentialCandidates(config) {
  const candidates = []
  if (config.credentialFile) candidates.push(resolve(config.credentialFile))
  if (process.env.TALARIA_DSH_BRIDGE) candidates.push(resolve(process.env.TALARIA_DSH_BRIDGE))
  candidates.push(resolve(config.cwd ?? process.cwd(), '.talaria/runtime/dsh-bridge.json'))
  candidates.push(resolve(homedir(), '.talaria/runtime/dsh-bridge.json'))
  return [...new Set(candidates)]
}

function assertLoopback(baseUrl) {
  let url
  try {
    url = new URL(baseUrl)
  } catch (error) {
    throw new BridgeError(`Talaria 桥接 base_url 无效：${baseUrl}`, { code: 'TALARIA_BRIDGE_CONFIG', cause: error })
  }
  if (url.protocol !== 'http:' || !LOOPBACK_HOSTS.has(url.hostname)) {
    throw new BridgeError(
      `Talaria 桥接必须使用回环 HTTP 地址，收到 ${url.origin}；拒绝非本机地址。`,
      { code: 'TALARIA_BRIDGE_CONFIG' },
    )
  }
  return url.origin
}

function readCredential(config) {
  if (config.baseUrl && config.token) {
    return { base_url: config.baseUrl, token: config.token }
  }
  for (const path of credentialCandidates(config)) {
    let raw
    try {
      raw = readFileSync(path, 'utf8')
    } catch {
      continue
    }
    let parsed
    try {
      parsed = JSON.parse(raw)
    } catch (error) {
      throw new BridgeError(`Talaria 桥接凭据不是合法 JSON：${path}`, {
        code: 'TALARIA_BRIDGE_CONFIG',
        cause: error,
      })
    }
    if (typeof parsed.base_url !== 'string' || typeof parsed.token !== 'string' || parsed.token.length === 0) {
      throw new BridgeError(`Talaria 桥接凭据缺少 base_url/token：${path}`, {
        code: 'TALARIA_BRIDGE_CONFIG',
      })
    }
    return parsed
  }
  throw new BridgeError(
    '未找到 Talaria 桥接凭据。请先运行 talaria up（会写入 .talaria/runtime/dsh-bridge.json），'
      + '或设置 TALARIA_DSH_BRIDGE 指向该文件。',
    { code: 'TALARIA_BRIDGE_UNAVAILABLE' },
  )
}

/** 创建只读桥：POST /api/snapshots，返回带 snapshot_id 的确定性快照元数据。 */
export function createBridge(config = {}) {
  const fetchImpl = config.fetchImpl ?? globalThis.fetch
  const cache = { path: null, mtimeMs: 0, value: null }

  function credential() {
    if (config.baseUrl && config.token) return { base_url: config.baseUrl, token: config.token }
    for (const path of credentialCandidates(config)) {
      let stat
      try {
        stat = statSync(path)
      } catch {
        continue
      }
      if (cache.path !== path || cache.mtimeMs !== stat.mtimeMs || cache.value === null) {
        cache.path = path
        cache.mtimeMs = stat.mtimeMs
        cache.value = readCredential({ ...config, credentialFile: path })
      }
      return cache.value
    }
    return readCredential(config)
  }

  async function createSnapshot(args, signal) {
    const credentials = credential()
    const baseUrl = assertLoopback(credentials.base_url)
    let response
    try {
      response = await fetchImpl(`${baseUrl}/api/snapshots`, {
        method: 'POST',
        headers: {
          Accept: 'application/json',
          'Content-Type': 'application/json',
          Authorization: `Bearer ${credentials.token}`,
        },
        body: JSON.stringify(args),
        signal,
      })
    } catch (error) {
      if (error?.name === 'AbortError') throw error
      throw new BridgeError(
        `无法连接 Talaria 守护进程 ${baseUrl}：请确认 talaria up 仍在运行。`,
        { code: 'TALARIA_BRIDGE_UNAVAILABLE', cause: error },
      )
    }
    let body = null
    try {
      body = await response.json()
    } catch {
      body = null
    }
    if (!response.ok) {
      const detail = typeof body?.message === 'string' ? body.message : `HTTP ${response.status}`
      throw new BridgeError(`Talaria 守护进程拒绝创建快照：${detail}`, {
        code: 'TALARIA_BRIDGE_HTTP',
      })
    }
    if (typeof body?.snapshot_id !== 'string' || body.snapshot_id.length === 0) {
      throw new BridgeError('Talaria 守护进程返回的快照缺少 snapshot_id，已拒绝该结果。', {
        code: 'TALARIA_BRIDGE_PROTOCOL',
      })
    }
    return body
  }

  return { createSnapshot, credential }
}

export const SNAPSHOT_OUTPUT_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  required: ['snapshot_id', 'instrument_id', 'timeframes', 'cutoff_ts', 'created_ts', 'mode', 'bar_counts'],
  properties: {
    snapshot_id: { type: 'string' },
    instrument_id: { type: 'string' },
    timeframes: { type: 'array', items: { type: 'string' } },
    cutoff_ts: { type: 'integer' },
    created_ts: { type: 'integer' },
    mode: { type: 'string', enum: ['HISTORICAL_PIT', 'OBSERVED_REPLAY'] },
    bar_counts: { type: 'object' },
  },
}

export function renderSnapshot(args, value) {
  const counts = Object.entries(value.bar_counts)
    .map(([timeframe, count]) => `${timeframe}:${count}`)
    .join('，')
  return [{
    type: 'text',
    text: `快照 ${value.snapshot_id}（${value.instrument_id}）已创建，截止 ${value.cutoff_ts}；K 线数量 ${counts}。后续读取请绑定该 snapshot_id。`,
  }]
}
