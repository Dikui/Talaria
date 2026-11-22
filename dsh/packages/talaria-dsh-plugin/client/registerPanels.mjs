export const panelId = (panel) => `talaria-${panel}`

/** Slot registrations follow DSH's plugin manager and built-in right-sidebar guide. */
export function registerPanels(ctx, labels, views) {
  const panels = Object.keys(labels)
  ctx.effect(() => ctx.slots.inject('main', function* () {
    for (const panel of panels) {
      yield ctx.slots.register({ name: 'main', key: panelId(panel) }, views.main(panel))
    }
    const stop = views.onMainRegistered()
    if (stop) yield stop
  }))

  ctx.effect(() => ctx.slots.inject('sidebar.panellist', function* () {
    for (const [index, panel] of panels.entries()) {
      yield ctx.slots.register({
        name: 'sidebar.panellist',
        id: panelId(panel),
        order: 10 + index,
        label: () => labels[panel],
      }, views.sidebarIcon(panel))
    }
  }))

  ctx.effect(() => {
    const tabs = panels.map(panel => panel === 'chart'
      ? { panel, kind: 'talaria-desk', title: 'Talaria 看盘' }
      : { panel, kind: panelId(panel), title: `Talaria ${labels[panel]}` })
    const disposers = tabs.map(({ kind, title, panel }, order) => ctx.sidebarRightTabs.register({
      id: `${kind}-tab`,
      kind,
      title: () => title,
      keepMounted: true,
      canOpen: () => true,
      guide: [{
        id: kind,
        order: 10 + order,
        title: () => title,
        icon: views.sidebarIcon(panel),
        ...(kind === 'talaria-desk' && {
          description: () => '在右侧面板打开确定性历史行情与指标工作台',
        }),
      }],
    }))
    const bodies = ctx.slots.inject('sidebar.right.pane.tab', function* () {
      for (const { kind, panel } of tabs) {
        yield ctx.slots.register({ name: 'sidebar.right.pane.tab', key: `${kind}-tab` }, views.sessionTab(panel))
      }
    })
    return () => {
      bodies()
      disposers.reverse().forEach(dispose => dispose())
    }
  })
}
