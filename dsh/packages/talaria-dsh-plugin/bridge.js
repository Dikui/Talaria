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

/**
 * 创建只读桥：所有工具都经回环 HTTP + Bearer 调用 Python 守护进程。
 * JS 侧只做参数拼装与 snapshot 绑定校验，绝不在此重算任何指标。
 */
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

  async function requestJson(method, path, { body, signal } = {}) {
    const credentials = credential()
    const baseUrl = assertLoopback(credentials.base_url)
    let response
    try {
      response = await fetchImpl(`${baseUrl}${path}`, {
        method,
        headers: {
          Accept: 'application/json',
          ...(body === undefined ? {} : { 'Content-Type': 'application/json' }),
          Authorization: `Bearer ${credentials.token}`,
        },
        ...(body === undefined ? {} : { body: JSON.stringify(body) }),
        signal,
      })
    } catch (error) {
      if (error?.name === 'AbortError') throw error
      throw new BridgeError(
        `无法连接 Talaria 守护进程 ${baseUrl}：请确认 talaria up 仍在运行。`,
        { code: 'TALARIA_BRIDGE_UNAVAILABLE', cause: error },
      )
    }
    let payload = null
    try {
      payload = await response.json()
    } catch {
      payload = null
    }
    if (!response.ok) {
      const detail = typeof payload?.message === 'string' ? payload.message : `HTTP ${response.status}`
      throw new BridgeError(`Talaria 守护进程拒绝请求 ${path}：${detail}`, {
        code: 'TALARIA_BRIDGE_HTTP',
      })
    }
    return payload
  }

  async function getSnapshot(snapshotId, signal) {
    if (typeof snapshotId !== 'string' || snapshotId.length === 0) {
      throw new BridgeError('缺少 snapshot_id，已拒绝读取。', { code: 'TALARIA_BRIDGE_PROTOCOL' })
    }
    const meta = await requestJson('GET', `/api/snapshots/${encodeURIComponent(snapshotId)}`, { signal })
    if (meta?.snapshot_id !== snapshotId) {
      throw new BridgeError('Talaria 返回的快照与请求的 snapshot_id 不一致，已拒绝该结果。', {
        code: 'TALARIA_BRIDGE_PROTOCOL',
      })
    }
    return meta
  }

  function assertBound(meta, { snapshot_id, instrument_id, timeframe }) {
    if (meta.snapshot_id !== snapshot_id) {
      throw new BridgeError('工具调用绑定的 snapshot_id 与快照不一致。', {
        code: 'TALARIA_BRIDGE_SNAPSHOT',
      })
    }
    if (instrument_id && meta.instrument_id !== instrument_id) {
      throw new BridgeError(
        `快照 ${snapshot_id} 属于 ${meta.instrument_id}，与请求的 ${instrument_id} 不一致。`,
        { code: 'TALARIA_BRIDGE_SNAPSHOT' },
      )
    }
    if (timeframe && !Array.isArray(meta.timeframes)) {
      throw new BridgeError('快照缺少 timeframes，已拒绝读取。', { code: 'TALARIA_BRIDGE_PROTOCOL' })
    }
    if (timeframe && !meta.timeframes.includes(timeframe)) {
      throw new BridgeError(`快照 ${snapshot_id} 未包含周期 ${timeframe}。`, {
        code: 'TALARIA_BRIDGE_SNAPSHOT',
      })
    }
  }

  function query(params) {
    const search = new URLSearchParams()
    for (const [key, value] of Object.entries(params)) {
      if (value === undefined || value === null || value === '') continue
      if (Array.isArray(value)) {
        if (value.length > 0) search.set(key, value.join(','))
        continue
      }
      search.set(key, String(value))
    }
    const text = search.toString()
    return text ? `?${text}` : ''
  }

  async function createSnapshot(args, signal) {
    const body = await requestJson('POST', '/api/snapshots', { body: args, signal })
    if (typeof body?.snapshot_id !== 'string' || body.snapshot_id.length === 0) {
      throw new BridgeError('Talaria 守护进程返回的快照缺少 snapshot_id，已拒绝该结果。', {
        code: 'TALARIA_BRIDGE_PROTOCOL',
      })
    }
    return body
  }

  async function getIndicators(args, signal) {
    const meta = await getSnapshot(args.snapshot_id, signal)
    assertBound(meta, args)
    const path = `/api/snapshots/${encodeURIComponent(args.snapshot_id)}/indicators${query({
      timeframe: args.timeframe,
      names: args.names,
      limit: args.limit,
    })}`
    const body = await requestJson('GET', path, { signal })
    if (body?.snapshot_id !== args.snapshot_id) {
      throw new BridgeError('指标结果没有绑定请求的 snapshot_id，已拒绝。', {
        code: 'TALARIA_BRIDGE_PROTOCOL',
      })
    }
    return body
  }

  async function getKeyLevels(args, signal) {
    const meta = await getSnapshot(args.snapshot_id, signal)
    assertBound(meta, args)
    const path = `/api/snapshots/${encodeURIComponent(args.snapshot_id)}/key-levels${query({
      timeframe: args.timeframe,
      categories: args.categories,
      limit: args.limit,
      min_strength: args.min_strength,
    })}`
    const body = await requestJson('GET', path, { signal })
    if (body?.snapshot_id !== args.snapshot_id) {
      throw new BridgeError('关键位结果没有绑定请求的 snapshot_id，已拒绝。', {
        code: 'TALARIA_BRIDGE_PROTOCOL',
      })
    }
    return body
  }

  async function computeOperator(args, signal) {
    const meta = await getSnapshot(args.snapshot_id, signal)
    assertBound(meta, args)
    const series = await requestJson('POST', '/api/operators/compute', {
      body: {
        snapshot_id: args.snapshot_id,
        timeframe: args.timeframe,
        operator: args.operator,
        params: args.params ?? {},
        limit: args.limit,
      },
      signal,
    })
    return {
      snapshot_id: args.snapshot_id,
      instrument_id: meta.instrument_id,
      timeframe: args.timeframe,
      operator: args.operator,
      series,
    }
  }

  async function getChartView(args, signal) {
    const meta = await getSnapshot(args.snapshot_id, signal)
    assertBound(meta, args)
    const view = await requestJson('GET', `/api/chart-view${query({
      instrument_id: args.instrument_id,
      timeframe: args.timeframe,
      from_ts: args.from_ts,
      to_ts: args.to_ts,
      price_min: args.price_min,
      price_max: args.price_max,
    })}`, { signal })
    return { snapshot_id: args.snapshot_id, ...view }
  }

  async function reportUsage(report, signal) {
    return requestJson('POST', '/api/collaboration/usage', { body: report, signal })
  }

  async function submitResult(consultationId, payload, signal) {
    return requestJson(
      'POST',
      `/api/collaboration/consultations/${encodeURIComponent(consultationId)}/result`,
      { body: payload, signal },
    )
  }

  return {
    createSnapshot,
    getSnapshot,
    getIndicators,
    getKeyLevels,
    computeOperator,
    getChartView,
    submitResult,
    reportUsage,
    credential,
  }
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

export const INDICATOR_BUNDLE_OUTPUT_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  required: ['snapshot_id', 'instrument_id', 'timeframe', 'series'],
  properties: {
    snapshot_id: { type: 'string' },
    instrument_id: { type: 'string' },
    timeframe: { type: 'string' },
    series: { type: 'array', items: { type: 'object', additionalProperties: true } },
  },
}

export const KEY_LEVEL_SET_OUTPUT_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  required: ['snapshot_id', 'instrument_id', 'timeframe', 'levels'],
  properties: {
    snapshot_id: { type: 'string' },
    instrument_id: { type: 'string' },
    timeframe: { type: 'string' },
    levels: { type: 'array', items: { type: 'object', additionalProperties: true } },
  },
}

export const OPERATOR_OUTPUT_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  required: ['snapshot_id', 'instrument_id', 'timeframe', 'operator', 'series'],
  properties: {
    snapshot_id: { type: 'string' },
    instrument_id: { type: 'string' },
    timeframe: { type: 'string' },
    operator: { type: 'string' },
    series: { type: 'object', additionalProperties: true },
  },
}

export const CHART_VIEW_OUTPUT_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  required: ['snapshot_id', 'instrument', 'timeframe', 'window', 'drawings'],
  properties: {
    snapshot_id: { type: 'string' },
    instrument: { type: 'object', additionalProperties: true },
    timeframe: { type: 'string' },
    window: { type: 'object', additionalProperties: true },
    drawings: { type: 'array', items: { type: 'object', additionalProperties: true } },
  },
}

function latestText(series) {
  const latest = series?.summary?.latest ?? {}
  const entries = Object.entries(latest)
    .filter(([, value]) => value !== null && value !== undefined)
    .map(([key, value]) => `${key}=${value}`)
  return entries.length > 0 ? entries.join('，') : '预热中（无有效值）'
}

export function renderIndicators(_args, value) {
  const lines = (value.series ?? []).map(
    (series) => `${series.name}：${latestText(series)}（有效点 ${series.summary?.valid_points ?? 0}）`,
  )
  return [{
    type: 'text',
    text: `快照 ${value.snapshot_id} · ${value.instrument_id} ${value.timeframe} 指标（Python 计算，十进制字符串）：\n${lines.join('\n')}`,
  }]
}

function levelPrice(level) {
  if (level.price !== null && level.price !== undefined) return level.price
  return `${level.price_low ?? '?'}–${level.price_high ?? '?'}`
}

export function renderKeyLevels(_args, value) {
  const lines = (value.levels ?? []).map(
    (level) => `${level.level_id} · ${level.kind} · ${level.category} · ${levelPrice(level)} · 强度 ${Number(level.strength).toFixed(2)} · ${level.evidence_class}`,
  )
  return [{
    type: 'text',
    text: `快照 ${value.snapshot_id} · ${value.instrument_id} ${value.timeframe} 关键位（${lines.length} 条，只读）：\n${lines.join('\n') || '无'}`,
  }]
}

export function renderOperator(_args, value) {
  const points = value.series?.points ?? []
  const last = points[points.length - 1]
  const values = last
    ? Object.entries(last.values ?? {})
        .filter(([, value]) => value !== null && value !== undefined)
        .map(([key, item]) => `${key}=${item}`)
        .join('，')
    : '无有效点'
  return [{
    type: 'text',
    text: `快照 ${value.snapshot_id} · ${value.instrument_id} ${value.timeframe} 算子 ${value.operator}（Python 计算）：${values}`,
  }]
}

export function renderChartView(_args, value) {
  const window = value.window ?? {}
  const drawings = (value.drawings ?? [])
    .map((drawing) => `${drawing.drawing_id}:${drawing.kind}`)
    .join('，')
  return [{
    type: 'text',
    text: `快照 ${value.snapshot_id} · ${value.instrument?.instrument_id ?? ''} ${value.timeframe} 视窗 ${window.from_ts}–${window.to_ts}，价格 ${window.price_min}–${window.price_max}；人工画线 ${drawings || '无'}。`,
  }]
}
