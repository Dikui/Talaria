export const panelId = (panel) => `talaria-${panel}`

/** The single main panel. All five workbench views live in its tablist. */
export const MAIN_PANEL_ID = 'talaria-workspace'

/** Existing rightbar tab kinds must stay stable for saved tab selections. */
export const RIGHTBAR_TABS = Object.freeze([
  { panel: 'chart', kind: 'talaria-desk' },
  { panel: 'alerts', kind: 'talaria-alerts' },
  { panel: 'scanner', kind: 'talaria-scanner' },
  { panel: 'levels', kind: 'talaria-levels' },
  { panel: 'accounts', kind: 'talaria-accounts' },
])

function registerWithRollback(registrations) {
  const disposers = []
  const dispose = () => disposers.splice(0).reverse().forEach(stop => stop())
  try {
    for (const stop of registrations()) disposers.push(stop)
  } catch (error) {
    dispose()
    throw error
  }
  return dispose
}

/**
 * Registers only the consultation rightbar session tabs (`talaria-desk`, etc.)
 * as an external Co-Pilot Analysis surface. Primary workbench hosting (`main`
 * and `sidebar.panellist`) is retired per ADR-0023 in favor of the standalone
 * Talaria Desk workbench.
 */
export function registerPanels(ctx, config, views) {
  const { tabs } = config
  const inject = (name, registrations) => ctx.slots.inject(name, () => registerWithRollback(registrations))

  ctx.effect(() => registerWithRollback(function* () {
    for (const [order, tab] of tabs.entries()) {
      yield ctx.sidebarRightTabs.register({
        id: `${tab.kind}-tab`,
        kind: tab.kind,
        title: tab.title,
        keepMounted: true,
        canOpen: () => true,
        guide: [{
          id: tab.kind,
          order: 10 + order,
          title: tab.title,
          icon: views.guideIcon ? views.guideIcon(tab.panel) : views.sidebarIcon?.(),
          ...(tab.description && { description: tab.description }),
        }],
      })
    }

    yield inject('sidebar.right.pane.tab', function* () {
      for (const tab of tabs) {
        yield ctx.slots.register({ name: 'sidebar.right.pane.tab', key: `${tab.kind}-tab` }, views.sessionTab(tab.panel))
      }
    })

    const stop = views.onRegistered?.()
    if (stop) yield stop
  }))
}
