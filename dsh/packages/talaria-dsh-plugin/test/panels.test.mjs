import { strict as assert } from 'node:assert'
import { test } from 'node:test'
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
