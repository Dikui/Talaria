export const WORKSPACE_TAB_KEY = 'talaria-dsh-workspace-tab'
export const DOCK_KEY = 'talaria-dsh-docked-panel'
export const LEGACY_ACTIVE_PANEL_KEY = 'talaria-dsh-main-panel'

const PANEL_PREFIX = 'talaria-'

function readValue(storage, key) {
  try {
    return storage('sessionStorage').getItem(key) ?? storage('localStorage').getItem(key)
  } catch {
    return null
  }
}

function writeValue(storage, key, value) {
  for (const kind of ['sessionStorage', 'localStorage']) {
    try {
      storage(kind).setItem(key, value)
    } catch {
      // A restricted storage context can still use the workspace.
    }
  }
}

/** Accepts a panel name, or a legacy panel id such as `talaria-alerts`. */
export function normalizePanelName(value, panels) {
  if (typeof value !== 'string' || !value) return null
  const name = value.startsWith(PANEL_PREFIX) ? value.slice(PANEL_PREFIX.length) : value
  return panels.includes(name) ? name : null
}

/** Restores the last workspace tab, migrating the old per-panel main choice. */
export function readWorkspaceTab(storage = kind => window[kind], panels) {
  const current = firstValidPanel(storage, WORKSPACE_TAB_KEY, panels)
  if (current) return current
  return firstValidPanel(storage, LEGACY_ACTIVE_PANEL_KEY, panels) ?? 'chart'
}

function firstValidPanel(storage, key, panels) {
  for (const kind of ['sessionStorage', 'localStorage']) {
    try {
      const panel = normalizePanelName(storage(kind).getItem(key), panels)
      if (panel) return panel
    } catch {
      // Try the other storage.
    }
  }
  return null
}

export function saveWorkspaceTab(panel, storage = kind => window[kind]) {
  writeValue(storage, WORKSPACE_TAB_KEY, panel)
}

/**
 * A live plugin reload can leave the host on an old per-panel main id. Seed
 * the workspace tab from that id only when no valid workspace preference is
 * saved; an existing tab choice is never overwritten.
 */
export function migrateLegacyCurrentTab(id, panels, storage = kind => window[kind]) {
  const panel = normalizePanelName(id, panels)
  if (!panel) return null
  if (firstValidPanel(storage, WORKSPACE_TAB_KEY, panels)) return null
  writeValue(storage, WORKSPACE_TAB_KEY, panel)
  return panel
}

export function readDockPanel(raw, panels) {
  const panel = normalizePanelName(raw, panels)
  return panel && panel !== 'chart' ? panel : null
}

export function readDockPreference(storage = kind => window[kind], panels) {
  return readDockPanel(readValue(storage, DOCK_KEY), panels)
}

export function saveDockPanel(panel, storage = kind => window[kind]) {
  writeValue(storage, DOCK_KEY, panel)
}

/** Roving-tab keyboard movement for a horizontal tablist. */
export function nextTabIndex(key, index, count) {
  if (count <= 0) return null
  if (key === 'ArrowRight') return (index + 1) % count
  if (key === 'ArrowLeft') return (index - 1 + count) % count
  if (key === 'Home') return 0
  if (key === 'End') return count - 1
  return null
}

export function tabDomId(panel) {
  return `talaria-workspace-tab-${panel}`
}

export function panelDomId(panel) {
  return `talaria-workspace-panel-${panel}`
}
