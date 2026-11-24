import { useEffect, useState, useSyncExternalStore } from "react";

import { createLink, type LinkSnapshot, type PanelLink } from "./link";
import type { PanelName } from "./panels";
import { openBroadcastTransport } from "./transport";

export function usePanelLink(options: {
  key: string;
  panel: PanelName;
  initialTimeframe?: string;
  syncTimeframe?: boolean;
}): PanelLink {
  const [link] = useState(() => {
    const transport = openBroadcastTransport();
    const storage = typeof localStorage !== "undefined" ? localStorage : undefined;
    const created = createLink({
      key: options.key,
      panel: options.panel,
      initialTimeframe: options.initialTimeframe,
      syncTimeframe: options.syncTimeframe,
      transport,
      storage,
    });
    return { created, transport };
  });

  useEffect(() => {
    link.created.start();
    return () => {
      link.created.stop();
      link.transport.close();
    };
  }, [link]);

  return link.created;
}

export function useLinkSnapshot(link: PanelLink): LinkSnapshot {
  return useSyncExternalStore(link.subscribe, link.getSnapshot, link.getSnapshot);
}

export function useLinkCrosshair(link: PanelLink): {
  ts: number | null;
  price?: number | null;
} {
  return useSyncExternalStore(
    link.subscribeCrosshair,
    link.getCrosshair,
    link.getCrosshair,
  );
}
