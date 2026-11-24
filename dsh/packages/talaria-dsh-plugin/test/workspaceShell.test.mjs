import { strict as assert } from 'node:assert'
import { readFileSync } from 'node:fs'
import { test } from 'node:test'

// ADR-0023: DSH client only renders consultation rightbar session tabs using
// a read-only connection boundary; primary main/sidebar workbench hosting is retired.
const source = readFileSync(new URL('../client/index.tsx', import.meta.url), 'utf8')
const messagesSource = readFileSync(new URL('../client/messages.mjs', import.meta.url), 'utf8')

test('hosts only consultation rightbar session tabs without primary workbench slots', () => {
  assert.doesNotMatch(source, /function WorkspaceView\(/)
  assert.doesNotMatch(source, /function Workspace\(/)
  assert.doesNotMatch(source, /MAIN_PANEL_ID/)
  assert.doesNotMatch(source, /"layout"/)
  assert.match(source, /sessionTab:\s*\(panel:\s*PanelName\)\s*=>/)
  assert.match(source, /fullWorkbench=\{false\}/)
  assert.match(source, /<Surface connection=\{connection\}>/)
  assert.match(messagesSource, /talaria-data up/)
})
