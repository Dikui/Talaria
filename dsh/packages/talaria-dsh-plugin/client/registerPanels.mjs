export const panelId = (panel) => `talaria-${panel}`

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

/** In rc.5, sidebar.panellist rows start pinned. The host owns their More menu. */
export function registerPanels(ctx, labels, views) {
  const panels = Object.keys(labels)
  const inject = (name, registrations) => ctx.slots.inject(name, () => registerWithRollback(registrations))

  ctx.effect(() => registerWithRollback(function* () {
    yield inject('main', function* () {
      for (const panel of panels) {
        yield ctx.slots.register({ name: 'main', key: panelId(panel) }, views.main(panel))
      }
    })

    yield inject('sidebar.panellist', function* () {
      for (const [index, panel] of panels.entries()) {
        yield ctx.slots.register({
          name: 'sidebar.panellist',
          id: panelId(panel),
          order: 10 + index,
          label: () => labels[panel],
        }, views.sidebarIcon(panel))
      }
    })

    const tabs = panels.map(panel => panel === 'chart'
      ? { panel, kind: 'talaria-desk', title: 'Talaria 看盘' }
      : { panel, kind: panelId(panel), title: `Talaria ${labels[panel]}` })
    for (const [order, { kind, title, panel }] of tabs.entries()) {
      yield ctx.sidebarRightTabs.register({
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
      })
    }
    yield inject('sidebar.right.pane.tab', function* () {
      for (const { kind, panel } of tabs) {
        yield ctx.slots.register({ name: 'sidebar.right.pane.tab', key: `${kind}-tab` }, views.sessionTab(panel))
      }
    })
    const stop = views.onMainRegistered()
    if (stop) yield stop
  }))
}
