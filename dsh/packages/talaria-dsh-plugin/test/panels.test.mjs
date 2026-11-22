import { strict as assert } from 'node:assert'
import { test } from 'node:test'
import { panelLinkKey, restoreActivePanel } from '../client/panelPreferences.mjs'
import { registerPanels } from '../client/registerPanels.mjs'

const panels = { chart: '图表', alerts: '预警', scanner: '扫描', levels: '关键位' }

// 平台 02：fake 使用已安装 DSH 的 keyed slot、list slot 和右栏 tab 注册契约。
test('registers root panels, matching sidebar entries and session right tabs', () => {
  const slots = new Map()
  const tabs = new Map()
  const cleanup = []
  const layout = { selectPanel(id) { assert.ok(slots.has(`main:${id}`)); this.selected = id } }
  const ctx = {
    layout,
    effect(apply) { cleanup.push(apply()) },
    slots: {
      inject(_slot, apply) {
        const result = apply()
        const disposers = typeof result === 'function' ? [result] : Array.from(result)
        return () => disposers.reverse().forEach(dispose => dispose())
      },
      register(options) {
        const name = `${options.name}:${options.key ?? options.id}`
        slots.set(name, options)
        return () => slots.delete(name)
      },
    },
    sidebarRightTabs: {
      register(definition) {
        tabs.set(definition.kind, definition)
        return () => tabs.delete(definition.kind)
      },
    },
  }

  registerPanels(ctx, panels, {
    main: () => null,
    sidebarIcon: () => null,
    sessionTab: () => null,
    onMainRegistered() { layout.selectPanel('talaria-alerts') },
  })

  assert.equal(layout.selected, 'talaria-alerts')
  for (const [name, label] of Object.entries(panels)) {
    const id = `talaria-${name}`
    assert.ok(slots.has(`main:${id}`))
    assert.equal(slots.get(`sidebar.panellist:${id}`).label(), label)
  }
  assert.deepEqual([...tabs.keys()], ['talaria-desk', 'talaria-alerts', 'talaria-scanner', 'talaria-levels'])
  for (const definition of tabs.values()) {
    assert.ok(slots.has(`sidebar.right.pane.tab:${definition.id}`))
  }
  cleanup.reverse().forEach(dispose => dispose())
  assert.equal(slots.size + tabs.size, 0)
})

// 平台 02 审查：宿主拒绝重复注册，失败的插件卸载后可以重新加载。
for (const failure of ['main:talaria-scanner', 'sidebar.panellist:talaria-scanner',
  'tab:talaria-scanner', 'sidebar.right.pane.tab:talaria-scanner-tab']) {
  test(`rolls back partial registrations and can reload after ${failure} fails`, () => {
    const registered = new Set()
    const effects = []
    const error = new Error('registration failed')
    let failing = true
    function register(id) {
      if (failing && id === failure) throw error
      assert.ok(!registered.has(id), `duplicate ${id}`)
      registered.add(id)
      return () => registered.delete(id)
    }
    const ctx = {
      effect(apply) { effects.push(apply()) },
      slots: {
        inject(_slot, apply) {
          const result = apply()
          const disposers = typeof result === 'function' ? [result] : Array.from(result)
          return () => disposers.reverse().forEach(dispose => dispose())
        },
        register: ({ name, key, id }) => register(`${name}:${key ?? id}`),
      },
      sidebarRightTabs: { register: ({ kind }) => register(`tab:${kind}`) },
    }
    const views = {
      main: () => null, sidebarIcon: () => null, sessionTab: () => null,
      onMainRegistered: () => register('active-panel-subscription'),
    }
    const unload = () => effects.splice(0).reverse().forEach(dispose => dispose())
    assert.throws(() => registerPanels(ctx, panels, views), error)
    unload()
    assert.deepEqual([...registered], [])

    failing = false
    registerPanels(ctx, panels, views)
    assert.equal(registered.size, 17)
    unload()
    assert.deepEqual([...registered], [])
  })
}

// 平台 02 审查：刷新时恢复仍在看的 Talaria 面板，返回会诊后保留会诊。
test('returning to the consultation clears the previously active panel before refresh', () => {
  const storage = () => {
    const values = new Map()
    return {
      getItem: key => values.get(key) ?? null,
      setItem: (key, value) => values.set(key, value),
      removeItem: key => values.delete(key),
    }
  }
  const tab = storage()
  const local = storage()
  function openLayout(tabPreferences = tab) {
    let activePanelId = null
    const listeners = new Set()
    const layout = {
      panelInfo: {
        getSnapshot: () => ({ activePanelId }),
        subscribe(listener) { listeners.add(listener); return () => listeners.delete(listener) },
      },
      selectPanel(id) {
        activePanelId = id
        listeners.forEach((listener) => listener())
      },
    }
    const close = restoreActivePanel(layout, Object.keys(panels),
      kind => kind === 'sessionStorage' ? tabPreferences : local)
    return { layout, close }
  }
  const first = openLayout()
  first.layout.selectPanel('talaria-alerts')
  first.close()
  const refreshed = openLayout()
  assert.equal(refreshed.layout.panelInfo.getSnapshot().activePanelId, 'talaria-alerts')
  refreshed.layout.selectPanel(null)
  refreshed.close()
  const consultation = openLayout()
  assert.equal(consultation.layout.panelInfo.getSnapshot().activePanelId, null)
  consultation.close()

  const otherWindow = openLayout(storage())
  otherWindow.layout.selectPanel('talaria-chart')
  otherWindow.close()
  const reopened = openLayout()
  assert.equal(reopened.layout.panelInfo.getSnapshot().activePanelId, null)
  reopened.close()
})

// 平台 02 审查：主区与停靠位置共用偏好，会诊会话各自保存。
test('docking preserves the panel group preference key', () => {
  assert.equal(panelLinkKey('alerts', 'dock'), panelLinkKey('alerts', 'main'))
})

test('consultation charts store preferences under their own session id', () => {
  assert.equal(panelLinkKey('chart', 'session', 'alpha'), 'session:alpha:chart')
  assert.notEqual(panelLinkKey('chart', 'session', 'alpha'), panelLinkKey('chart', 'session', 'beta'))
})

test('restores accounts as a chart dock destination', async () => {
  const { dockPanels } = await import('../client/panelPreferences.mjs')
  assert.deepEqual(dockPanels(['chart', 'accounts', 'levels']), ['accounts', 'levels'])
})
