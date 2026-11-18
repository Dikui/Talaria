/**
 * talaria-desk 的单调执行守卫。
 *
 * 两层：
 * 1. 只读白名单：除看盘工具与只读检索外一律拒绝，兜住 preset 漏配的执行工具。
 * 2. fork 重放：fork 会话继承了父会话的历史轨迹；若新调用与历史中的写操作
 *    name+参数完全一致，则拒绝，避免从历史轨迹重放已完成的写操作。
 *
 * 守卫返回 string 即拒绝，返回 undefined 表示不改变已有决策；它没有 allow
 * 结果，因此不可能把上游的拒绝翻回允许。
 */

export const READ_ONLY_TOOLS = Object.freeze(new Set([
  'get_snapshot',
  'read',
  'glob',
  'grep',
  'web_search',
  'web_fetch',
  'todo_write',
  'ask_user_question',
]))

function normalize(value) {
  if (Array.isArray(value)) return value.map(normalize)
  if (value && typeof value === 'object') {
    const result = {}
    for (const key of Object.keys(value).sort()) {
      if (value[key] !== undefined) result[key] = normalize(value[key])
    }
    return result
  }
  return value
}

export function stableArguments(value) {
  return JSON.stringify(normalize(value ?? {}))
}

export function callFingerprint(name, args) {
  return `${name}\u0000${stableArguments(args)}`
}

/** 读取 fork 前缀里的 tool/call 指纹；只读，不修改会话。 */
export function inheritedWriteFingerprints(session, isWrite = (name) => !READ_ONLY_TOOLS.has(name)) {
  const fingerprints = new Set()
  if (!session?.header?.isSeeded) return fingerprints
  const cut = session.inheritedEventCount
  if (!Number.isSafeInteger(cut) || cut <= 0 || typeof session.eventAt !== 'function') {
    return fingerprints
  }
  for (let seq = 0; seq < cut; seq += 1) {
    const event = session.eventAt(seq)
    if (event?.type !== 'tool/call') continue
    const { name, arguments: raw } = event.data
    if (typeof name !== 'string' || isWrite(name) === false) continue
    let args = {}
    try {
      args = raw ? JSON.parse(raw) : {}
    } catch {
      args = raw
    }
    fingerprints.add(callFingerprint(name, args))
  }
  return fingerprints
}

export function createDeskGuard({ getSession, allowlist = READ_ONLY_TOOLS } = {}) {
  return function talariaDeskGuard(execution) {
    if (allowlist.has(execution.name)) {
      return undefined
    }
    const session = getSession?.(execution)
    if (session?.header?.isSeeded) {
      const history = inheritedWriteFingerprints(session)
      if (history.has(callFingerprint(execution.name, execution.arguments))) {
        return `talaria-desk 单调守卫：${execution.name} 与 fork 前历史轨迹中的写操作完全相同，拒绝重放该历史写操作。`
      }
    }
    return `talaria-desk 权限边界：${execution.name} 不在只读工具白名单内，已拦截；看盘请使用 get_snapshot。`
  }
}
