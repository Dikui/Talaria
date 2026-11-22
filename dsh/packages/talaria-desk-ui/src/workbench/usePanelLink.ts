import { useEffect, useState, useSyncExternalStore } from "react";
import { createLink, type LinkTransport, type PanelLink } from "./link";
import type { PanelName } from "./panels";
import { openBroadcastTransport } from "./transport";

function browserStorage(kind: "localStorage" | "sessionStorage") {
  try {
    return window[kind];
  } catch {
    return undefined;
  }
}

export function usePanelLink(panel: PanelName, key: string, transport?: LinkTransport) {
  const [link, setLink] = useState<PanelLink | null>(null);
  useEffect(() => {
    const ownedTransport = transport ? undefined : openBroadcastTransport();
    const current = createLink({
      key,
      panel,
      transport: transport ?? ownedTransport!,
      storage: browserStorage("localStorage"),
      preferences: browserStorage("sessionStorage"),
    });
    setLink(current);
    window.addEventListener("pagehide", current.leave);
    window.addEventListener("pageshow", current.join);
    return () => {
      window.removeEventListener("pagehide", current.leave);
      window.removeEventListener("pageshow", current.join);
      current.close();
      ownedTransport?.close();
    };
  }, [panel, key, transport]);
  return link;
}

export function useLinkSnapshot(link: PanelLink) {
  return useSyncExternalStore(link.subscribe, link.getSnapshot);
}
