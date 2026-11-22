import { LINK_CONNECT, LINK_PATH, LINK_READY, selectionPrecedes, type LinkMessage, type LinkTransport } from "@talaria/desk-ui";

export function createLinkRelay(baseUrl: string) {
  const listeners = new Set<(message: LinkMessage) => void>();
  const readyListeners = new Set<() => void>();
  const url = new URL(LINK_PATH, baseUrl);
  const frame = document.createElement("iframe");
  frame.hidden = true;
  frame.title = "Talaria 面板联动";
  frame.referrerPolicy = "origin";
  frame.sandbox.add("allow-scripts", "allow-same-origin");
  const pendingSelections = new Map<string, Extract<LinkMessage, { type: "locate" | "symbol" }>>();
  const pendingDrawings = new Map<string, LinkMessage>();
  let port: MessagePort | null = null;
  let ready = false;

  frame.addEventListener("load", () => {
    port?.close();
    ready = false;
    const channel = new MessageChannel();
    port = channel.port1;
    port.onmessage = ({ data }: MessageEvent<typeof LINK_READY | LinkMessage>) => {
      if (data === LINK_READY) {
        ready = true;
        pendingSelections.forEach((message) => port?.postMessage(message));
        pendingSelections.clear();
        pendingDrawings.forEach((message) => port?.postMessage(message));
        pendingDrawings.clear();
        readyListeners.forEach((listener) => listener());
      } else {
        listeners.forEach((listener) => listener(data));
      }
    };
    frame.contentWindow?.postMessage(LINK_CONNECT, url.origin, [channel.port2]);
  });
  frame.src = url.href;
  document.body.append(frame);

  return {
    send(message) {
      if (ready) port?.postMessage(message);
      else if (message.type === "locate" || (message.type === "symbol" && message.to === undefined)) {
        const previous = pendingSelections.get(message.group);
        if (!previous || selectionPrecedes(previous.selection, message.selection)) {
          pendingSelections.set(message.group, message);
        }
      } else if (message.type === "drawings-changed") {
        pendingDrawings.set(JSON.stringify([message.group, message.instrumentId]), message);
      }
      listeners.forEach((listener) => listener(message));
    },
    subscribe(listener) {
      listeners.add(listener);
      return () => { listeners.delete(listener); };
    },
    onReady(listener) {
      readyListeners.add(listener);
      return () => { readyListeners.delete(listener); };
    },
    close() {
      port?.close();
      frame.remove();
      listeners.clear();
      readyListeners.clear();
    },
  } satisfies LinkTransport & { close: () => void };
}
