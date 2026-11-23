import { MAIN_PANEL_ID, panelId } from './registerPanels.mjs'
import { migrateLegacyCurrentTab } from './workspaceTabs.mjs'

export const ACTIVE_PANEL_KEY = 'talaria-dsh-main-panel'

function normalizeActivePanelId(id, panels) {
  if (id === MAIN_PANEL_ID) return MAIN_PANEL_ID
  return panels.some(panel => panelId(panel) === id) ? MAIN_PANEL_ID : null
}

/**
 * Restores the workspace after a refresh. Every legacy per-panel main choice
 * (`talaria-chart`, `talaria-alerts`, …) now opens the single workspace panel.
 * Leaving to the consultation clears the preference so a refresh stays there.
 */
export function restoreActivePanel(layout, panels, storage = kind => window[kind]) {
  const active = layout.panelInfo
  const remember = () => {
    const raw = active.getSnapshot().activePanelId
    const id = normalizeActivePanelId(raw, panels)
    for (const kind of ['sessionStorage', 'localStorage']) {
      try {
        // Keep the consultation choice for this tab even if another tab saves a panel later.
        if (raw === null && kind === 'sessionStorage') storage(kind).setItem(ACTIVE_PANEL_KEY, '')
        else if (raw === null) storage(kind).removeItem(ACTIVE_PANEL_KEY)
        else if (id) storage(kind).setItem(ACTIVE_PANEL_KEY, id)
      } catch {
        // A restricted storage context can still use each panel.
      }
    }
  }
  let saved = null
  try {
    saved = storage('sessionStorage').getItem(ACTIVE_PANEL_KEY) ?? storage('localStorage').getItem(ACTIVE_PANEL_KEY)
  } catch {
    // No saved panel can be restored.
  }
  const restore = normalizeActivePanelId(saved, panels)
  const current = active.getSnapshot().activePanelId
  if (current === null && restore) layout.selectPanel(restore)
  // A live plugin reload can keep an old per-panel id as the host's active
  // panel; move it to the single workspace instead of rendering a blank main.
  else if (current !== null && current !== MAIN_PANEL_ID && normalizeActivePanelId(current, panels)) {
    // Keep the tab the user was actually looking at when upgrading in place.
    migrateLegacyCurrentTab(current, panels, storage)
    layout.selectPanel(MAIN_PANEL_ID)
  }
  return active.subscribe(remember)
}

/** @template {string} T @param {T[]} panels */
export function dockPanels(panels) {
  return panels.filter(panel => panel !== 'chart')
}

export function panelLinkKey(panel, surface, sessionId) {
  if (surface === 'session') return `session:${sessionId}:${panel}`
  return `main:${panel}`
}
