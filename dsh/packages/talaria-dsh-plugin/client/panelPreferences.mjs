import { panelId } from './registerPanels.mjs'

const ACTIVE_PANEL_KEY = 'talaria-dsh-main-panel'

export function restoreActivePanel(layout, panels, storage = kind => window[kind]) {
  const active = layout.panelInfo
  const isTalaria = id => panels.some(panel => panelId(panel) === id)
  const remember = () => {
    const id = active.getSnapshot().activePanelId
    for (const kind of ['sessionStorage', 'localStorage']) {
      try {
        // Keep the consultation choice for this tab even if another tab saves a panel later.
        if (id === null && kind === 'sessionStorage') storage(kind).setItem(ACTIVE_PANEL_KEY, '')
        else if (id === null) storage(kind).removeItem(ACTIVE_PANEL_KEY)
        else if (isTalaria(id)) storage(kind).setItem(ACTIVE_PANEL_KEY, id)
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
  if (active.getSnapshot().activePanelId === null && isTalaria(saved)) layout.selectPanel(saved)
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
