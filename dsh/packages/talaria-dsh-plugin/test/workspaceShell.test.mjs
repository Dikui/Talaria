import { strict as assert } from 'node:assert'
import { readFileSync } from 'node:fs'
import { test } from 'node:test'

// This package has no DOM renderer, so guard the composition that keeps the
// offline regression out: the brand/tab bar is rendered before the one
// connection boundary, and only the selected tab's content sits inside it.
const source = readFileSync(new URL('../client/index.tsx', import.meta.url), 'utf8')

function between(start, end) {
  const from = source.indexOf(start)
  const to = source.indexOf(end, from + start.length)
  assert.ok(from >= 0 && to > from, `missing ${start} … ${end}`)
  return source.slice(from, to)
}

test('keeps the workspace tab bar outside the single connection boundary', () => {
  const view = between('function WorkspaceView(', 'function Workspace(')
  const tablist = view.indexOf('role="tablist"')
  const boundary = view.indexOf('<Surface chrome={false}')
  assert.ok(tablist >= 0, 'workspace tablist is missing')
  assert.ok(boundary > tablist, 'connection boundary must follow the tab bar')

  const content = view.slice(boundary)
  assert.match(content, /role="tabpanel"/)
  assert.match(content, /ChartWithDock/)
  assert.match(content, /WorkbenchPanel/)

  const workspace = between('function Workspace(', 'function hostPanelLabel')
  assert.match(workspace, /<WorkspaceView/)
  assert.doesNotMatch(workspace, /<Surface[\s\S]*<WorkspaceView/)
})
