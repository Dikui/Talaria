import { readFileSync } from 'node:fs'
import { isIPv4 } from 'node:net'
import { homedir } from 'node:os'
import { basename, dirname, resolve } from 'node:path'

export class BridgeError extends Error {
  constructor(message, options = {}) {
    super(message)
    this.name = 'BridgeError'
    this.code = options.code ?? 'TALARIA_BRIDGE_ERROR'
    this.cause = options.cause
  }
}

export function offlineGuidance(baseUrl) {
  if (baseUrl) {
    return `无法连接 Talaria 数据引擎 ${baseUrl}：请确认 talaria-data 正在运行，或打开独立看盘工作台。(Cannot connect to talaria-data at ${baseUrl}: ensure talaria-data is running or open Talaria Desk.)`
  }
  return 'Talaria 数据引擎未运行（未找到 service-descriptor.json）。请打开 Talaria 独立看盘工作台或运行 talaria-data up。(Talaria data engine talaria-data is offline: service-descriptor.json not found. Open Talaria Desk or run talaria-data up.)'
}

function credentialCandidates(config) {
  const configured = process.env.TALARIA_DSH_BRIDGE
    || process.env.TALARIA_SERVICE_DESCRIPTOR
    || config.serviceDescriptor
    || config.credentialFile
  if (configured) {
    const resolved = resolve(configured)
    if (basename(resolved) === 'dsh-bridge.json') {
      return [resolved, resolve(dirname(resolved), 'service-descriptor.json')]
    }
    return [resolved]
  }
  const dataDir = config.dataDir || process.env.TALARIA_DATA_DIR
  if (dataDir) {
    const root = resolve(dataDir)
    return [
      resolve(root, 'runtime/service-descriptor.json'),
      resolve(root, 'runtime/dsh-bridge.json'),
    ]
  }
  const cwd = config.cwd ?? process.cwd()
  return [...new Set([
    resolve(cwd, '.talaria/runtime/service-descriptor.json'),
    resolve(homedir(), '.talaria/runtime/service-descriptor.json'),
    resolve(cwd, '.talaria/runtime/dsh-bridge.json'),
    resolve(homedir(), '.talaria/runtime/dsh-bridge.json'),
  ])]
}

export function assertLoopback(baseUrl) {
  let url
  try {
    url = new URL(baseUrl)
  } catch (error) {
    throw new BridgeError(`Talaria 桥接 base_url 无效：${baseUrl}`, { code: 'TALARIA_BRIDGE_CONFIG', cause: error })
  }
  const { hostname } = url
  const loopback = hostname === 'localhost' || hostname === '[::1]'
    || (isIPv4(hostname) && hostname.startsWith('127.'))
  if (url.protocol !== 'http:' || !loopback || url.username || url.password) {
    throw new BridgeError(
      `Talaria 桥接必须使用回环 HTTP 地址，收到 ${url.origin}；拒绝非本机地址。`,
      { code: 'TALARIA_BRIDGE_CONFIG' },
    )
  }
  return url.origin
}

function readDescriptorFiles(config) {
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
      throw new BridgeError(`Talaria 服务描述不是合法 JSON：${path}`, {
        code: 'TALARIA_BRIDGE_CONFIG',
        cause: error,
      })
    }
    if (!parsed || typeof parsed.base_url !== 'string' || typeof parsed.token !== 'string' || parsed.token.length === 0) {
      throw new BridgeError(`Talaria 服务描述缺少 base_url/token：${path}`, {
        code: 'TALARIA_BRIDGE_CONFIG',
      })
    }
    return parsed
  }
  return null
}

function readCredential(config) {
  const explicitUrl = config.baseUrl || process.env.TALARIA_SERVICE_URL
  const explicitToken = config.token || process.env.TALARIA_SERVICE_TOKEN
  if (explicitUrl && explicitToken) {
    return {
      base_url: explicitUrl,
      token: explicitToken,
      ...(typeof config.dataDirId === 'string' && config.dataDirId ? { data_dir_id: config.dataDirId } : {}),
    }
  }
  const descriptor = readDescriptorFiles(config)
  if (explicitUrl) {
    if (descriptor?.token) {
      return {
        ...descriptor,
        base_url: explicitUrl,
      }
    }
    throw new BridgeError(
      '已配置 TALARIA_SERVICE_URL / baseUrl，但缺少访问 token 且未找到 service-descriptor.json。(Service URL configured without token and service-descriptor.json was not found.)',
      { code: 'TALARIA_BRIDGE_CONFIG' },
    )
  }
  if (descriptor) {
    return descriptor
  }
  throw new BridgeError(
    offlineGuidance(),
    { code: 'TALARIA_BRIDGE_UNAVAILABLE' },
  )
}

/**
 * 创建只读协同桥：按 service-descriptor.json（或显式 URL）通过回环 HTTP + Bearer 调用 talaria-data。
 * JS 侧只做参数拼装与 snapshot 绑定校验，不托管数据服务生命周期，也不在本地重算任何指标。
 */
export function createBridge(config = {}) {
  const fetchImpl = config.fetchImpl ?? globalThis.fetch
  function credential() {
    const parsed = readCredential(config)
    const result = { base_url: assertLoopback(parsed.base_url), token: parsed.token }
    if (typeof parsed.data_dir_id === 'string' && parsed.data_dir_id.length > 0) {
      result.data_dir_id = parsed.data_dir_id
    }
    return result
  }

  async function requestJson(method, path, { body, signal } = {}) {
    const credentials = credential()
    const baseUrl = credentials.base_url
    let response
    try {
      response = await fetchImpl(`${baseUrl}${path}`, {
        method,
        headers: {
          Accept: 'application/json',
          ...(body === undefined ? {} : { 'Content-Type': 'application/json' }),
          Authorization: `Bearer ${credentials.token}`,
          ...(credentials.data_dir_id ? { 'X-Talaria-Data-Dir': credentials.data_dir_id } : {}),
        },
        ...(body === undefined ? {} : { body: JSON.stringify(body) }),
        signal,
      })
    } catch (error) {
      if (error?.name === 'AbortError') throw error
      throw new BridgeError(
        offlineGuidance(baseUrl),
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

  async function getStructures(args, signal) {
    const meta = await getSnapshot(args.snapshot_id, signal)
    assertBound(meta, args)
    const path = `/api/snapshots/${encodeURIComponent(args.snapshot_id)}/structures${query({
      timeframe: args.timeframe,
      families: args.families,
      categories: args.categories,
      detector_ids: args.detector_ids,
      htf: args.htf,
      scale: args.scale,
      evidence_class: args.evidence_class,
      limit: args.limit,
      min_strength: args.min_strength,
      order: args.order,
    })}`
    const body = await requestJson('GET', path, { signal })
    if (body?.snapshot_id !== args.snapshot_id) {
      throw new BridgeError('市场结构结果没有绑定请求的 snapshot_id，已拒绝。', {
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

  async function writeAgentOverlay(args, signal) {
    const meta = await getSnapshot(args.snapshot_id, signal)
    assertBound(meta, args)
    const body = await requestJson('POST', '/api/annotations', { body: args, signal })
    if (body?.snapshot_id !== args.snapshot_id) {
      throw new BridgeError('标注结果没有绑定请求的 snapshot_id，已拒绝。', {
        code: 'TALARIA_BRIDGE_PROTOCOL',
      })
    }
    if (body?.status !== 'ACTIVE') {
      throw new BridgeError('标注未以 ACTIVE 状态落库，已拒绝该结果。', {
        code: 'TALARIA_BRIDGE_PROTOCOL',
      })
    }
    return body
  }

  async function createAlertDraft(args, signal) {
    const meta = await getSnapshot(args.snapshot_id, signal)
    assertBound(meta, args)
    const body = await requestJson('POST', '/api/alerts', {
      body: {
        instrument_id: args.instrument_id,
        timeframe: args.timeframe,
        condition: args.condition,
        note: args.note ?? '',
        repeat: args.repeat ?? true,
        cooldown_ms: args.cooldown_ms ?? 60000,
        expires_at: args.expires_at ?? null,
        draft: true,
      },
      signal,
    })
    if (body?.status !== 'DRAFT') {
      throw new BridgeError('预警未以 DRAFT 状态创建；智能体只能提交草案，已拒绝该结果。', {
        code: 'TALARIA_BRIDGE_PROTOCOL',
      })
    }
    return body
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

  async function submitBriefingResult(briefingId, payload, signal) {
    return requestJson(
      'POST',
      `/api/scanner/briefings/${encodeURIComponent(briefingId)}/result`,
      { body: payload, signal },
    )
  }

  return {
    createSnapshot,
    getSnapshot,
    getIndicators,
    getKeyLevels,
    getStructures,
    computeOperator,
    getChartView,
    writeAgentOverlay,
    createAlertDraft,
    submitResult,
    submitBriefingResult,
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

export const STRUCTURE_SET_OUTPUT_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  required: ['snapshot_id', 'instrument_id', 'timeframe', 'structures'],
  properties: {
    snapshot_id: { type: 'string' },
    instrument_id: { type: 'string' },
    timeframe: { type: 'string' },
    structures: { type: 'array', items: { type: 'object', additionalProperties: true } },
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

function structureGeomSummary(geometry) {
  if (!geometry) return '-'
  if (geometry.kind === 'HORIZONTAL_LINE') return `${geometry.price}`
  if (geometry.kind === 'HORIZONTAL_ZONE') return `${geometry.price_low}–${geometry.price_high}`
  if (geometry.kind === 'SLOPE_LINE') {
    return `${geometry.anchor_start?.price}@${geometry.anchor_start?.ts} → ${geometry.anchor_end?.price}@${geometry.anchor_end?.ts} (${geometry.scale ?? 'LINEAR'})`
  }
  if (geometry.kind === 'CHANNEL') {
    return `通道[${geometry.lower?.anchor_end?.price ?? '?'}–${geometry.upper?.anchor_end?.price ?? '?'}]`
  }
  return geometry.kind ?? '-'
}

export function renderStructures(_args, value) {
  const lines = (value.structures ?? []).map((st) => {
    const tfTag = st.source_timeframe && st.source_timeframe !== st.timeframe ? `[${st.source_timeframe}] ` : ''
    return `${st.structure_id} · ${tfTag}${st.family}:${st.kind} (${st.detector_id}) · ${structureGeomSummary(st.geometry)} · 强度 ${Number(st.strength).toFixed(2)} · ${st.status} · ${st.evidence_class}`
  })
  return [{
    type: 'text',
    text: `快照 ${value.snapshot_id} · ${value.instrument_id} ${value.timeframe} 市场结构（${lines.length} 条，只读）：\n${lines.join('\n') || '无'}`,
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


export const AGENT_ANNOTATION_OUTPUT_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  required: [
    'annotation_id',
    'snapshot_id',
    'instrument_id',
    'timeframe',
    'session_id',
    'round',
    'status',
    'label',
    'price_low',
    'price_high',
    'ts_anchor',
    'evidence_refs',
  ],
  properties: {
    annotation_id: { type: 'string' },
    snapshot_id: { type: 'string' },
    instrument_id: { type: 'string' },
    timeframe: { type: 'string' },
    session_id: { type: 'string' },
    round: { type: 'integer' },
    status: { type: 'string', enum: ['ACTIVE', 'ADOPTED', 'DISMISSED', 'ARCHIVED'] },
    label: { type: 'string' },
    price_low: { type: 'string' },
    price_high: { type: 'string' },
    ts_anchor: { type: 'integer' },
    evidence_refs: { type: 'array', items: { type: 'object', additionalProperties: true } },
  },
}

export const ALERT_DRAFT_OUTPUT_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  required: ['alert_id', 'instrument_id', 'timeframe', 'status', 'note'],
  properties: {
    alert_id: { type: 'string' },
    instrument_id: { type: 'string' },
    timeframe: { type: 'string' },
    status: { type: 'string', enum: ['DRAFT', 'ARMED', 'COOLDOWN', 'EXPIRED', 'DISABLED'] },
    note: { type: 'string' },
  },
}

export function renderAgentAnnotation(_args, value) {
  const evidence = (value.evidence_refs ?? [])
    .map((item) => item.level_id ?? `K线 ${item.kline_ts}`)
    .join('，')
  return [{
    type: 'text',
    text: `智能体标注 ${value.annotation_id} 已写入快照 ${value.snapshot_id}（${value.instrument_id} ${value.timeframe}），第 ${value.round} 轮；价格 ${value.price_low}–${value.price_high}，证据 ${evidence || '无'}。该标注为虚线建议，需人工采纳后才会成为画线。`,
  }]
}

export function renderAlertDraft(_args, value) {
  return [{
    type: 'text',
    text: `预警草案 ${value.alert_id}（${value.instrument_id} ${value.timeframe}）已创建，状态 ${value.status}；请人工在预警面板确认后才会激活，草案本身不会触发。`,
  }]
}
