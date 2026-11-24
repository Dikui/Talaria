import { DEFAULT_INSTRUMENT, type PanelName } from "./panels";

export const GROUP_LABELS = {
  red: "红组",
  blue: "蓝组",
  green: "绿组",
  none: "不联动",
} as const;

export type LinkGroup = keyof typeof GROUP_LABELS;
export const LINK_CHANNEL = "talaria-workbench-v1";

type StorageAccess = Pick<Storage, "getItem" | "setItem">;
type Selection = { instrumentId: string; timeframe: string };
type VersionedSelection = Selection & {
  revision: number;
  author: string;
  locateTs?: number | null;
};
type GroupMessage = { from: string; group: LinkGroup };

export type LocateTarget = {
  instrument_id: string;
  timeframe: string;
  triggered_ts: number;
};

export type Peer = Selection & GroupMessage & { panel: PanelName };

type LinkPayload =
  | { type: "symbol"; selection: VersionedSelection; syncTimeframe?: boolean; to?: string }
  | { type: "locate"; selection: VersionedSelection; ts: number }
  | { type: "crosshair"; ts: number | null; price?: number | null }
  | { type: "drawings-changed"; instrumentId: string }
  | { type: "ask"; selection?: VersionedSelection }
  | { type: "presence"; peer: Peer }
  | { type: "bye" };

export type LinkMessage = GroupMessage & LinkPayload;

export interface LinkTransport {
  send(message: LinkMessage): void;
  subscribe(listener: (message: LinkMessage) => void): () => void;
  onReady?(listener: () => void): () => void;
}

export interface LinkSnapshot extends Selection {
  group: LinkGroup;
  locateTs: number | null;
  peers: Peer[];
}

interface LinkOptions {
  key: string;
  panel: PanelName;
  transport: LinkTransport;
  initialTimeframe?: string;
  syncTimeframe?: boolean;
  storage?: StorageAccess;
  preferences?: StorageAccess;
}

const INITIAL: VersionedSelection = {
  instrumentId: DEFAULT_INSTRUMENT,
  timeframe: "1m",
  revision: 0,
  author: "",
};

const HEARTBEAT_MS = 1_500;
const PEER_TTL_MS = 4_000;

function read(storage: StorageAccess | undefined, key: string): string | null {
  try {
    return storage?.getItem(key) ?? null;
  } catch {
    return null;
  }
}

function save(storage: StorageAccess | undefined, key: string, value: string): void {
  try {
    storage?.setItem(key, value);
  } catch {
    // Ignore storage unavailability.
  }
}

function isGroup(group: string | null): group is LinkGroup {
  return group !== null && Object.hasOwn(GROUP_LABELS, group);
}

function selectionKey(group: LinkGroup, key: string): string {
  return `talaria-selection:${group === "none" ? key : group}`;
}

function readSelection(storage: StorageAccess | undefined, key: string): VersionedSelection {
  try {
    const value = JSON.parse(read(storage, key) ?? "null");
    if (
      value &&
      typeof value.instrumentId === "string" &&
      typeof value.timeframe === "string" &&
      Number.isFinite(value.revision) &&
      typeof value.author === "string"
    ) {
      return value;
    }
  } catch {
    // Ignore malformed saved selection.
  }
  return INITIAL;
}

export function selectionPrecedes(
  left: VersionedSelection,
  right: VersionedSelection,
): boolean {
  return (
    left.revision < right.revision ||
    (left.revision === right.revision && left.author < right.author)
  );
}

function rememberSelection(
  storage: StorageAccess | undefined,
  key: string,
  selection: VersionedSelection,
): void {
  if (selectionPrecedes(readSelection(storage, key), selection)) {
    save(storage, key, JSON.stringify(selection));
  }
}

export function cacheRelaySelection(
  storage: StorageAccess | undefined,
  message: LinkMessage,
): void {
  if (message.group !== "none" && "selection" in message && message.selection) {
    const selection =
      message.type === "locate"
        ? { ...message.selection, locateTs: message.ts }
        : message.selection;
    rememberSelection(storage, selectionKey(message.group, ""), selection);
  }
}

export function cachedRelayReply(
  storage: StorageAccess | undefined,
  message: LinkMessage,
): LinkMessage | undefined {
  if (message.type !== "ask" || message.group === "none") return;
  const selection = readSelection(storage, selectionKey(message.group, ""));
  if (!selectionPrecedes(message.selection ?? INITIAL, selection)) return;
  return { type: "symbol", group: message.group, from: "relay", selection };
}

export function createLink({
  key,
  panel,
  transport,
  initialTimeframe,
  syncTimeframe = true,
  storage,
  preferences = storage,
}: LinkOptions) {
  const from =
    typeof globalThis.crypto?.randomUUID === "function"
      ? globalThis.crypto.randomUUID()
      : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  const preferenceKey = `talaria-link-group:${key}`;
  const savedGroup = read(preferences, preferenceKey);
  let group: LinkGroup = isGroup(savedGroup) ? savedGroup : "red";
  let followTimeframe = syncTimeframe;

  const selectionStorage = () => (group === "none" ? preferences : storage);
  const loaded = readSelection(selectionStorage(), selectionKey(group, key));
  let selection: VersionedSelection = initialTimeframe
    ? { ...loaded, timeframe: initialTimeframe }
    : loaded;
  let locateDismissed = false;
  let crosshair: { ts: number | null; price?: number | null } = { ts: null };
  let crosshairOwner: string | null = null;
  let lastSentCrosshair: number | null = null;
  let pendingCrosshair: { ts: number | null; price?: number | null } | null = null;
  let crosshairFrame: number | undefined;
  let peerList: Peer[] = [];
  let joined = false;
  const peers = new Map<string, { peer: Peer; seen: number }>();
  const listeners = new Set<() => void>();
  const crosshairListeners = new Set<() => void>();
  const drawingListeners = new Set<(instrumentId: string) => void>();

  const snapshot = (): LinkSnapshot => ({
    instrumentId: selection.instrumentId,
    timeframe: selection.timeframe,
    group,
    locateTs: locateDismissed ? null : (selection.locateTs ?? null),
    peers: peerList,
  });
  let current = snapshot();

  function publish() {
    current = snapshot();
    listeners.forEach((listener) => listener());
  }

  function updatePeers() {
    peerList = Array.from(peers.values(), ({ peer }) => peer);
    publish();
  }

  function send(message: LinkPayload) {
    transport.send({ ...message, from, group });
  }

  function beat() {
    send({
      type: "presence",
      peer: {
        from,
        group,
        panel,
        instrumentId: selection.instrumentId,
        timeframe: selection.timeframe,
      },
    });
  }

  function updateCrosshair(ts: number | null, price?: number | null) {
    if (crosshair.ts === ts && crosshair.price === price) return;
    crosshair = { ts, price };
    crosshairListeners.forEach((listener) => listener());
  }

  function cancelCrosshair() {
    if (crosshairFrame !== undefined) {
      cancelAnimationFrame(crosshairFrame);
      crosshairFrame = undefined;
    }
    pendingCrosshair = null;
  }

  function setSelection(next: VersionedSelection, applyTimeframe = true) {
    selection = applyTimeframe
      ? next
      : { ...next, timeframe: selection.timeframe };
    locateDismissed = false;
    crosshairOwner = null;
    cancelCrosshair();
    updateCrosshair(null);
    rememberSelection(selectionStorage(), selectionKey(group, key), selection);
    publish();
  }

  function applySelection(next: Selection, ts: number | null, broadcastTf = followTimeframe) {
    const saved = readSelection(selectionStorage(), selectionKey(group, key));
    const nextSelection: VersionedSelection = {
      ...next,
      revision: Math.max(Date.now(), selection.revision + 1, saved.revision + 1),
      author: from,
      locateTs: ts,
    };
    setSelection(nextSelection, true);
    if (group !== "none") {
      if (ts === null) {
        send({ type: "symbol", selection: nextSelection, syncTimeframe: broadcastTf });
      } else {
        send({ type: "locate", selection: nextSelection, ts });
      }
    }
    beat();
  }

  function join() {
    joined = true;
    const saved = readSelection(selectionStorage(), selectionKey(group, key));
    if (selectionPrecedes(selection, saved)) {
      setSelection(saved, followTimeframe);
    }
    beat();
    if (group !== "none") {
      send({ type: "ask", selection });
    }
  }

  const stopTransport = transport.subscribe((message) => {
    if (message.from === from) return;
    if (message.type === "drawings-changed") {
      drawingListeners.forEach((listener) => listener(message.instrumentId));
      return;
    }
    if (message.type === "bye") {
      if (peers.delete(message.from)) updatePeers();
      if (crosshairOwner === message.from) {
        crosshairOwner = null;
        updateCrosshair(null);
      }
      return;
    }
    if (message.type === "presence") {
      const existing = peers.get(message.from)?.peer;
      const changed =
        !existing ||
        existing.group !== message.peer.group ||
        existing.instrumentId !== message.peer.instrumentId ||
        existing.timeframe !== message.peer.timeframe ||
        existing.panel !== message.peer.panel;
      peers.set(message.from, { peer: message.peer, seen: Date.now() });
      if (changed) updatePeers();
      return;
    }

    if (group === "none" || message.group !== group) return;

    if (message.type === "ask") {
      if (!message.selection || selectionPrecedes(message.selection, selection)) {
        send({ type: "symbol", selection, syncTimeframe: false, to: message.from });
      }
      return;
    }
    if (message.type === "crosshair") {
      crosshairOwner = message.ts === null ? null : message.from;
      updateCrosshair(message.ts, message.price);
      return;
    }
    if (message.type === "symbol") {
      if (message.to && message.to !== from) return;
      if (selectionPrecedes(selection, message.selection)) {
        const shouldApplyTf = followTimeframe && message.syncTimeframe !== false;
        setSelection(message.selection, shouldApplyTf);
      }
      return;
    }
    if (message.type === "locate") {
      if (selectionPrecedes(selection, message.selection)) {
        setSelection({ ...message.selection, locateTs: message.ts }, true);
      }
    }
  });

  const stopReady = transport.onReady?.(() => {
    if (joined) join();
  });

  let heartbeat: ReturnType<typeof setInterval> | undefined;

  return {
    getSnapshot: () => current,
    subscribe(listener: () => void) {
      listeners.add(listener);
      return () => {
        listeners.delete(listener);
      };
    },
    start() {
      join();
      heartbeat = setInterval(() => {
        const now = Date.now();
        let pruned = false;
        for (const [id, entry] of peers) {
          if (now - entry.seen > PEER_TTL_MS) {
            peers.delete(id);
            pruned = true;
          }
        }
        if (pruned) updatePeers();
        beat();
      }, HEARTBEAT_MS);
    },
    stop() {
      if (heartbeat !== undefined) {
        clearInterval(heartbeat);
        heartbeat = undefined;
      }
      joined = false;
      cancelCrosshair();
      send({ type: "bye" });
      stopReady?.();
      stopTransport();
    },
    setSymbol(instrumentId: string, timeframe = selection.timeframe) {
      applySelection(
        { instrumentId, timeframe },
        null,
        timeframe !== selection.timeframe ? followTimeframe : false,
      );
    },
    setTimeframe(timeframe: string, broadcast = followTimeframe) {
      applySelection(
        { instrumentId: selection.instrumentId, timeframe },
        null,
        broadcast,
      );
    },
    setSyncTimeframe(next: boolean) {
      followTimeframe = next;
    },
    locate(target: LocateTarget) {
      applySelection(
        { instrumentId: target.instrument_id, timeframe: target.timeframe },
        target.triggered_ts,
        true,
      );
    },
    clearLocate() {
      locateDismissed = true;
      publish();
    },
    setGroup(next: LinkGroup) {
      if (next === group) return;
      group = next;
      save(preferences, preferenceKey, next);
      const saved = readSelection(selectionStorage(), selectionKey(group, key));
      if (group === "none") {
        selection = saved;
      } else if (selectionPrecedes(selection, saved)) {
        selection = saved;
      }
      locateDismissed = false;
      crosshairOwner = null;
      cancelCrosshair();
      updateCrosshair(null);
      publish();
      beat();
      if (group !== "none") {
        send({ type: "ask", selection });
      }
    },
    sendCrosshair(ts: number | null, price?: number | null) {
      if (group === "none") return;
      if (ts === null && lastSentCrosshair === null) return;
      pendingCrosshair = { ts, price };
      if (crosshairFrame !== undefined) return;
      crosshairFrame = requestAnimationFrame(() => {
        crosshairFrame = undefined;
        const next = pendingCrosshair;
        pendingCrosshair = null;
        if (!next || next.ts === lastSentCrosshair) return;
        lastSentCrosshair = next.ts;
        send({ type: "crosshair", ts: next.ts, price: next.price });
      });
    },
    getCrosshair: () => crosshair,
    subscribeCrosshair(listener: () => void) {
      crosshairListeners.add(listener);
      return () => {
        crosshairListeners.delete(listener);
      };
    },
    notifyDrawingsChanged(instrumentId: string) {
      transport.send({
        type: "drawings-changed",
        from,
        group,
        instrumentId,
      });
    },
    subscribeDrawings(instrumentId: string, listener: () => void) {
      const wrapped = (changedId: string) => {
        if (changedId === instrumentId) listener();
      };
      drawingListeners.add(wrapped);
      return () => {
        drawingListeners.delete(wrapped);
      };
    },
  };
}

export type PanelLink = ReturnType<typeof createLink>;
