import { cacheRelaySelection, cachedRelayReply, LINK_CHANNEL, type LinkMessage, type LinkTransport } from "./link";

export const LINK_CONNECT = "talaria:link-connect";
export const LINK_READY = "talaria:link-ready";

export function openBroadcastTransport() {
  const channel = new BroadcastChannel(LINK_CHANNEL);
  return {
    send: (message) => channel.postMessage(message),
    subscribe(listener) {
      const receive = ({ data }: MessageEvent<LinkMessage>) => listener(data);
      channel.addEventListener("message", receive);
      return () => channel.removeEventListener("message", receive);
    },
    close: () => channel.close(),
  } satisfies LinkTransport & { close: () => void };
}

/** Use the resident-service origin for the same BroadcastChannel as the fallback pages. */
export function connectEmbeddedRelay(): void {
  if (window.parent === window) return;
  const referrer = document.referrer ? new URL(document.referrer) : null;
  const webOrigin = referrer && ["127.0.0.1", "localhost", "[::1]"].includes(referrer.hostname)
    ? referrer.origin : null;

  window.addEventListener("message", function connect(event) {
    if (
      event.source !== window.parent ||
      (event.origin !== "dsh-app://app" && event.origin !== webOrigin) ||
      event.data !== LINK_CONNECT || event.ports.length !== 1
    ) return;
    window.removeEventListener("message", connect);
    const port = event.ports[0];
    const transport = openBroadcastTransport();
    const unsubscribe = transport.subscribe((message) => port.postMessage(message));
    let storage: Storage | undefined;
    try {
      storage = window.localStorage;
    } catch {
      // The relay can still join the live channel when persistence is unavailable.
    }
    port.onmessage = ({ data }: MessageEvent<LinkMessage>) => {
      cacheRelaySelection(storage, data);
      const cached = cachedRelayReply(storage, data);
      if (cached) {
        port.postMessage(cached);
        transport.send(cached);
      }
      transport.send(data);
    };
    port.postMessage(LINK_READY);
    window.addEventListener("pagehide", function close(event) {
      if (event.persisted) return;
      window.removeEventListener("pagehide", close);
      unsubscribe();
      transport.close();
      port.close();
    });
  });
}
