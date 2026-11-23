import { strict as assert } from 'node:assert'
import { test } from 'node:test'
import {
  DOCK_KEY,
  WORKSPACE_TAB_KEY,
  migrateLegacyCurrentTab,
  nextTabIndex,
  panelDomId,
  readDockPanel,
  readWorkspaceTab,
  saveDockPanel,
  saveWorkspaceTab,
  tabDomId,
} from '../client/workspaceTabs.mjs'

const panels = ['chart', 'alerts', 'scanner', 'levels', 'accounts']
const LEGACY_ACTIVE_PANEL_KEY = 'talaria-dsh-main-panel'

function storage(initial = {}) {
  const values = new Map(Object.entries(initial))
  return {
    values,
    getItem: key => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value),
    removeItem: key => values.delete(key),
  }
}

function storages(session = {}, local = {}) {
  const byKind = { sessionStorage: storage(session), localStorage: storage(local) }
  return { byKind, read: kind => byKind[kind] }
}

test('reads the workspace tab from either storage, preferring the session', () => {
  const { read } = storages({ [WORKSPACE_TAB_KEY]: 'alerts' }, { [WORKSPACE_TAB_KEY]: 'levels' })
  assert.equal(readWorkspaceTab(read, panels), 'alerts')
  const invalidSession = storages({ [WORKSPACE_TAB_KEY]: 'talaria-unknown' }, { [WORKSPACE_TAB_KEY]: 'levels' })
  assert.equal(readWorkspaceTab(invalidSession.read, panels), 'levels')
})

test('migrates the legacy main panel id into a workspace tab', () => {
  const { read } = storages()
  assert.equal(readWorkspaceTab(read, panels), 'chart')
  const legacy = storages({ [LEGACY_ACTIVE_PANEL_KEY]: 'talaria-accounts' })
  assert.equal(readWorkspaceTab(legacy.read, panels), 'accounts')
  const legacyChart = storages({ [LEGACY_ACTIVE_PANEL_KEY]: 'talaria-chart' })
  assert.equal(readWorkspaceTab(legacyChart.read, panels), 'chart')
  const workspace = storages({ [LEGACY_ACTIVE_PANEL_KEY]: 'talaria-workspace' })
  assert.equal(readWorkspaceTab(workspace.read, panels), 'chart')
})

test('ignores unknown, empty and consultation-only saved values', () => {
  for (const value of ['', 'talaria-unknown', 'unknown', 'talaria-workspace']) {
    const { read } = storages({ [LEGACY_ACTIVE_PANEL_KEY]: value })
    assert.equal(readWorkspaceTab(read, panels), 'chart', value)
  }
  assert.equal(readWorkspaceTab(() => ({ getItem: () => { throw new Error('denied') } }), panels), 'chart')
})

test('saves the workspace tab in both storages so a restart restores it', () => {
  const { byKind } = storages()
  saveWorkspaceTab('scanner', kind => byKind[kind])
  assert.equal(byKind.sessionStorage.getItem(WORKSPACE_TAB_KEY), 'scanner')
  assert.equal(byKind.localStorage.getItem(WORKSPACE_TAB_KEY), 'scanner')
})

test('migrates a live legacy panel only when no valid workspace tab exists', () => {
  const empty = storages()
  assert.equal(migrateLegacyCurrentTab('talaria-scanner', panels, empty.read), 'scanner')
  assert.equal(empty.byKind.sessionStorage.getItem(WORKSPACE_TAB_KEY), 'scanner')
  assert.equal(empty.byKind.localStorage.getItem(WORKSPACE_TAB_KEY), 'scanner')

  const existing = storages({ [WORKSPACE_TAB_KEY]: 'alerts' })
  assert.equal(migrateLegacyCurrentTab('talaria-scanner', panels, existing.read), null)
  assert.equal(existing.byKind.sessionStorage.getItem(WORKSPACE_TAB_KEY), 'alerts')
  assert.equal(existing.byKind.localStorage.getItem(WORKSPACE_TAB_KEY), null)

  const expired = storages({ [WORKSPACE_TAB_KEY]: 'talaria-unknown' })
  assert.equal(migrateLegacyCurrentTab('talaria-levels', panels, expired.read), 'levels')
  assert.equal(expired.byKind.sessionStorage.getItem(WORKSPACE_TAB_KEY), 'levels')

  assert.equal(migrateLegacyCurrentTab('talaria-workspace', panels, empty.read), null)
  assert.equal(migrateLegacyCurrentTab('', panels, empty.read), null)
})

test('restores a dock panel name and accepts the legacy prefixed form', () => {
  assert.equal(readDockPanel('alerts', panels), 'alerts')
  assert.equal(readDockPanel('talaria-levels', panels), 'levels')
  assert.equal(readDockPanel('chart', panels), null)
  assert.equal(readDockPanel('talaria-desk', panels), null)
  assert.equal(readDockPanel('', panels), null)
})

test('saves the dock choice under the existing dock key', () => {
  const { byKind } = storages()
  saveDockPanel('accounts', kind => byKind[kind])
  assert.equal(byKind.sessionStorage.getItem(DOCK_KEY), 'accounts')
  assert.equal(byKind.localStorage.getItem(DOCK_KEY), 'accounts')
})

test('moves tab focus with arrows, Home and End and wraps at both ends', () => {
  assert.equal(nextTabIndex('ArrowRight', 0, panels.length), 1)
  assert.equal(nextTabIndex('ArrowRight', panels.length - 1, panels.length), 0)
  assert.equal(nextTabIndex('ArrowLeft', 0, panels.length), panels.length - 1)
  assert.equal(nextTabIndex('ArrowLeft', 2, panels.length), 1)
  assert.equal(nextTabIndex('Home', 3, panels.length), 0)
  assert.equal(nextTabIndex('End', 1, panels.length), panels.length - 1)
  assert.equal(nextTabIndex('Enter', 1, panels.length), null)
  assert.equal(nextTabIndex('ArrowRight', 0, 0), null)
})

test('derives stable tab and tabpanel ids from the panel name', () => {
  assert.equal(tabDomId('levels'), 'talaria-workspace-tab-levels')
  assert.equal(panelDomId('levels'), 'talaria-workspace-panel-levels')
})
