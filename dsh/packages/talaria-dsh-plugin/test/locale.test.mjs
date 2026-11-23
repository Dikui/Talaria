import { strict as assert } from 'node:assert'
import { test } from 'node:test'
import { MESSAGES } from '../client/messages.mjs'
import { normalizeHostLocale, resolveHostText, syncHostLocale } from '../client/localeSync.mjs'

test('normalizes DSH locale ids to the two desk-ui locales', () => {
  assert.equal(normalizeHostLocale('zh'), 'zh')
  assert.equal(normalizeHostLocale('zh-CN'), 'zh')
  assert.equal(normalizeHostLocale('zh_TW'), 'zh')
  assert.equal(normalizeHostLocale('en'), 'en')
  assert.equal(normalizeHostLocale('en-US'), 'en')
  assert.equal(normalizeHostLocale('ja'), 'en')
  assert.equal(normalizeHostLocale(null), 'en')
  assert.equal(normalizeHostLocale(undefined), 'en')
})

test('applies the initial host locale and keeps following switches', () => {
  let active = 'zh-CN'
  const listeners = new Set()
  const host = {
    getSnapshot: () => ({ active }),
    subscribe(listener) {
      listeners.add(listener)
      return () => listeners.delete(listener)
    },
  }
  const applied = []
  const stop = syncHostLocale(host, locale => applied.push(locale))
  assert.deepEqual(applied, ['zh'])

  active = 'en-US'
  listeners.forEach(listener => listener())
  assert.deepEqual(applied, ['zh', 'en'])

  active = 'ja'
  listeners.forEach(listener => listener())
  assert.deepEqual(applied, ['zh', 'en', 'en'])

  stop()
  active = 'zh'
  listeners.forEach(listener => listener())
  assert.deepEqual(applied, ['zh', 'en', 'en'])
  assert.equal(listeners.size, 0)
})

test('tolerates a missing locale runtime', () => {
  const applied = []
  const stop = syncHostLocale(undefined, locale => applied.push(locale))
  assert.deepEqual(applied, [])
  assert.doesNotThrow(stop)
})

test('resolves host-side copy through the locale service with a fallback', () => {
  const zh = { zh: '中文', en: 'English' }
  assert.equal(resolveHostText({ resolveText: () => 'resolved' }, zh), 'resolved')
  assert.equal(resolveHostText({ getSnapshot: () => ({ active: 'zh-CN' }) }, zh), '中文')
  assert.equal(resolveHostText({ getSnapshot: () => ({ active: 'en-US' }) }, zh), 'English')
  assert.equal(resolveHostText({ getSnapshot: () => ({ active: 'ja' }) }, zh), 'English')
  assert.equal(resolveHostText(undefined, zh), 'English')
})

test('every visible message has Chinese and English copy', () => {
  const keys = Object.keys(MESSAGES)
  assert.ok(keys.length >= 12)
  for (const key of keys) {
    const entry = MESSAGES[key]
    assert.equal(typeof entry.zh, 'string', key)
    assert.equal(typeof entry.en, 'string', key)
    assert.ok(entry.zh.trim(), `${key}.zh`)
    assert.ok(entry.en.trim(), `${key}.en`)
  }
})
