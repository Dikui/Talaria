import { strict as assert } from 'node:assert'
import { test } from 'node:test'
import { ACTIVE_PANEL_KEY, panelLinkKey, restoreActivePanel } from '../client/panelPreferences.mjs'
import { MAIN_PANEL_ID, RIGHTBAR_TABS, registerPanels } from '../client/registerPanels.mjs'
import { WORKSPACE_TAB_KEY } from '../client/workspaceTabs.mjs'

const panels = ['chart', 'alerts', 'scanner', 'levels', 'accounts']
const config = {
  mainId: MAIN_PANEL_ID,
  sidebarLabel: () => 'Talaria 工作台',
  tabs: RIGHTBAR_TABS.map(({ panel, kind }) => ({
    panel,
    kind,
    title: () => panel === 'chart' ? 'Talaria 看盘' : `Talaria ${panel}`,
    ...(panel === 'chart' ? { description: () => '在右侧面板打开工作台' } : {}),
  })),
}

// ADR-0023: DSH only mounts optional rightbar session tabs (no primary main or sidebar.panellist).
test('registers only the five consultation rightbar tabs without main or sidebar hosting', () => {
  const slots = new Map()
  const tabs = new Map()
  const cleanup = []
  const ctx = {
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

  registerPanels(ctx, config, {
    sidebarIcon: () => null,
    guideIcon: panel => `guide:${panel}`,
    sessionTab: () => null,
  })

  assert.deepEqual([...slots.keys()].filter(key => key.startsWith('main:')), [])
  assert.deepEqual([...slots.keys()].filter(key => key.startsWith('sidebar.panellist:')), [])
  assert.deepEqual([...tabs.keys()], [
    'talaria-desk', 'talaria-alerts', 'talaria-scanner', 'talaria-levels', 'talaria-accounts',
  ])
  const guide = tabs.get('talaria-desk').guide
  assert.equal(guide.length, 1)
  assert.equal(guide[0].title(), 'Talaria 看盘')
  assert.equal(guide[0].description(), '在右侧面板打开工作台')
  assert.equal(tabs.get('talaria-accounts').guide[0].description, undefined)
  assert.deepEqual([...tabs.values()].map(definition => definition.guide[0].icon), [
    'guide:chart', 'guide:alerts', 'guide:scanner', 'guide:levels', 'guide:accounts',
  ])
  for (const definition of tabs.values()) {
    assert.ok(slots.has(`sidebar.right.pane.tab:${definition.id}`))
  }
  cleanup.reverse().forEach(dispose => dispose())
  assert.equal(slots.size + tabs.size, 0)
})

// 平台 02 审查：宿主拒绝重复注册，失败的插件卸载后可以重新加载。
for (const failure of ['tab:talaria-alerts', 'sidebar.right.pane.tab:talaria-alerts-tab']) {
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
      sidebarIcon: () => null, sessionTab: () => null,
      onRegistered: () => register('post-register-hook'),
    }
    const unload = () => effects.splice(0).reverse().forEach(dispose => dispose())
    assert.throws(() => registerPanels(ctx, config, views), error)
    unload()
    assert.deepEqual([...registered], [])

    failing = false
    registerPanels(ctx, config, views)
    assert.equal(registered.size, 11)
    unload()
    assert.deepEqual([...registered], [])
  })
}

function fakeLayout(initialPanelId = null) {
  let activePanelId = initialPanelId
  const listeners = new Set()
  return {
    layout: {
      panelInfo: {
        getSnapshot: () => ({ activePanelId }),
        subscribe(listener) { listeners.add(listener); return () => listeners.delete(listener) },
      },
      selectPanel(id) {
        activePanelId = id
        listeners.forEach((listener) => listener())
      },
    },
  }
}

function storage(values = new Map()) {
  return {
    getItem: key => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value),
    removeItem: key => values.delete(key),
  }
}

// 平台 02 审查：刷新时恢复工作台，返回会诊后保留会诊。
test('returning to the consultation clears the previously active panel before refresh', () => {
  const tab = storage()
  const local = storage()
  const read = kind => kind === 'sessionStorage' ? tab : local
  function openLayout(storageRead = read) {
    const { layout } = fakeLayout()
    const close = restoreActivePanel(layout, panels, storageRead)
    return { layout, close }
  }
  const first = openLayout()
  first.layout.selectPanel(MAIN_PANEL_ID)
  first.close()
  const refreshed = openLayout()
  assert.equal(refreshed.layout.panelInfo.getSnapshot().activePanelId, MAIN_PANEL_ID)
  refreshed.layout.selectPanel(null)
  refreshed.close()
  const consultation = openLayout()
  assert.equal(consultation.layout.panelInfo.getSnapshot().activePanelId, null)
  consultation.close()

  const otherWindow = openLayout(kind => kind === 'sessionStorage' ? storage() : read(kind))
  otherWindow.layout.selectPanel('talaria-chart')
  otherWindow.close()
  const reopened = openLayout()
  assert.equal(reopened.layout.panelInfo.getSnapshot().activePanelId, null)
  reopened.close()
})

test('migrates every legacy Talaria main panel id to the workspace panel', () => {
  for (const legacy of ['talaria-chart', 'talaria-alerts', 'talaria-accounts', MAIN_PANEL_ID]) {
    const local = storage(new Map([[ACTIVE_PANEL_KEY, legacy]]))
    const { layout } = fakeLayout()
    const close = restoreActivePanel(layout, panels, kind => kind === 'sessionStorage' ? storage() : local)
    assert.equal(layout.panelInfo.getSnapshot().activePanelId, MAIN_PANEL_ID, legacy)
    close()
  }
})

test('moves an already-active legacy panel id onto the workspace', () => {
  const { layout } = fakeLayout('talaria-scanner')
  const close = restoreActivePanel(layout, panels, kind => kind === 'sessionStorage' ? storage() : storage())
  assert.equal(layout.panelInfo.getSnapshot().activePanelId, MAIN_PANEL_ID)
  close()
})

test('seeds the workspace tab from a hot-reloaded legacy panel when none is saved', () => {
  const session = storage()
  const local = storage()
  const { layout } = fakeLayout('talaria-scanner')
  const close = restoreActivePanel(layout, panels, kind => kind === 'sessionStorage' ? session : local)
  assert.equal(layout.panelInfo.getSnapshot().activePanelId, MAIN_PANEL_ID)
  assert.equal(session.getItem(WORKSPACE_TAB_KEY), 'scanner')
  assert.equal(local.getItem(WORKSPACE_TAB_KEY), 'scanner')
  close()
})

test('keeps a valid workspace tab when migrating a hot-reloaded legacy panel', () => {
  const session = storage(new Map([[WORKSPACE_TAB_KEY, 'alerts']]))
  const local = storage()
  const { layout } = fakeLayout('talaria-scanner')
  const close = restoreActivePanel(layout, panels, kind => kind === 'sessionStorage' ? session : local)
  assert.equal(layout.panelInfo.getSnapshot().activePanelId, MAIN_PANEL_ID)
  assert.equal(session.getItem(WORKSPACE_TAB_KEY), 'alerts')
  assert.equal(local.getItem(WORKSPACE_TAB_KEY), null)
  close()
})

test('replaces an expired workspace tab with the hot-reloaded legacy panel', () => {
  const session = storage(new Map([[WORKSPACE_TAB_KEY, 'talaria-unknown']]))
  const local = storage(new Map([[WORKSPACE_TAB_KEY, 'talaria-workspace']]))
  const { layout } = fakeLayout('talaria-levels')
  const close = restoreActivePanel(layout, panels, kind => kind === 'sessionStorage' ? session : local)
  assert.equal(layout.panelInfo.getSnapshot().activePanelId, MAIN_PANEL_ID)
  assert.equal(session.getItem(WORKSPACE_TAB_KEY), 'levels')
  assert.equal(local.getItem(WORKSPACE_TAB_KEY), 'levels')
  close()
})

test('leaves a cold start without a workspace tab on the default chart tab', () => {
  const session = storage()
  const local = storage()
  const { layout } = fakeLayout()
  const close = restoreActivePanel(layout, panels, kind => kind === 'sessionStorage' ? session : local)
  assert.equal(layout.panelInfo.getSnapshot().activePanelId, null)
  assert.equal(session.getItem(WORKSPACE_TAB_KEY), null)
  assert.equal(local.getItem(WORKSPACE_TAB_KEY), null)
  close()
})

test('ignores a saved value that is not a Talaria panel', () => {
  const local = storage(new Map([[ACTIVE_PANEL_KEY, 'conversation']]))
  const { layout } = fakeLayout()
  const close = restoreActivePanel(layout, panels, kind => kind === 'sessionStorage' ? storage() : local)
  assert.equal(layout.panelInfo.getSnapshot().activePanelId, null)
  close()
})

// 平台 02 审查：主区与停靠位置共用偏好，会诊会话各自保存。
test('docking preserves the panel group preference key', () => {
  assert.equal(panelLinkKey('alerts', 'dock'), panelLinkKey('alerts', 'main'))
})

test('consultation charts store preferences under their own session id', () => {
  assert.equal(panelLinkKey('chart', 'session', 'alpha'), 'session:alpha:chart')
  assert.notEqual(panelLinkKey('chart', 'session', 'alpha'), panelLinkKey('chart', 'session', 'beta'))
})
