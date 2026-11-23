/** DSH locale ids are BCP 47; desk-ui exposes the Chinese/English pair. */
export function normalizeHostLocale(raw) {
  return typeof raw === 'string' && /^zh(?:[-_]|$)/i.test(raw) ? 'zh' : 'en'
}

/**
 * Mirror DSH's active locale into desk-ui and keep following host switches.
 * Returns the unsubscribe callback for the plugin effect.
 */
export function syncHostLocale(host, apply, normalize = normalizeHostLocale) {
  if (!host || typeof host.getSnapshot !== 'function' || typeof host.subscribe !== 'function') {
    return () => {}
  }
  const push = () => apply(normalize(host.getSnapshot().active))
  push()
  return host.subscribe(push)
}

/**
 * Resolve copy owned by the host (sidebar labels, rightbar titles and guide
 * entries) with the host locale service. Unknown host locales fall back to en,
 * matching DSH's own fallback chain.
 */
export function resolveHostText(host, text) {
  if (host && typeof host.resolveText === 'function') return host.resolveText(text)
  return normalizeHostLocale(host?.getSnapshot?.().active) === 'zh' ? text.zh : text.en
}
