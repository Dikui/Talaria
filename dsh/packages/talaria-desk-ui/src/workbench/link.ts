import { DEFAULT_INSTRUMENT, type PanelName } from "./panels";

export const GROUP_LABELS = { red: "红组", blue: "蓝组", green: "绿组", none: "不联动" } as const;
export type LinkGroup = keyof typeof GROUP_LABELS;
export const LINK_CHANNEL = "talaria-workbench-v1";

type StorageAccess = Pick<Storage, "getItem" | "setItem">;
type Selection = { instrumentId: string; timeframe: string };
type VersionedSelection = Selection & { revision: number; author: string };
type GroupMessage = { from: string; group: LinkGroup };

export type LocateTarget = {
  instrument_id: string;
  timeframe: string;
  triggered_ts: number;
};

export type Peer = Selection & GroupMessage & { panel: PanelName };
type LinkPayload =
  | { type: "symbol"; selection: VersionedSelection; to?: string }
  | { type: "locate"; selection: VersionedSelection; ts: number }
  | { type: "crosshair"; ts: number | null }
  | { type: "ask" }
  | { type: "presence"; peer: Peer }
  | { type: "bye" };

export type LinkMessage = GroupMessage & LinkPayload;

export interface LinkTransport {
  send(message: LinkMessage): void;
  subscribe(listener: (message: LinkMessage) => void): () => void;
  onReady?(listener: () => void): () => void;
}

interface LinkSnapshot extends Selection {
  group: LinkGroup;
  locateTs: number | null;
  crosshair: number | null;
  peers: Peer[];
}

interface LinkOptions {
  key: string;
  panel: PanelName;
  transport: LinkTransport;
  storage?: StorageAccess;
  preferences?: StorageAccess;
}

const INITIAL: VersionedSelection = {
  instrumentId: DEFAULT_INSTRUMENT, timeframe: "1m", revision: 0, author: "",
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

function save(storage: StorageAccess | undefined, key: string, value: string) {
  try {
    storage?.setItem(key, value);
  } catch {
    // Linking still works when storage is unavailable.
  }
}

function isGroup(group: string | null): group is LinkGroup {
  return group !== null && Object.hasOwn(GROUP_LABELS, group);
}

function selectionKey(group: LinkGroup, key: string) {
  return `talaria-selection:${group === "none" ? key : group}`;
}

function readSelection(storage: StorageAccess | undefined, key: string): VersionedSelection {
  try {
    const value = JSON.parse(read(storage, key) ?? "null");
    if (
      value && typeof value.instrumentId === "string" && typeof value.timeframe === "string" &&
      Number.isFinite(value.revision) && typeof value.author === "string"
    ) {
      return value;
    }
  } catch {
    // Recover from a malformed saved selection.
  }
  return INITIAL;
}

function precedes(left: VersionedSelection, right: VersionedSelection) {
  return left.revision < right.revision || (left.revision === right.revision && left.author < right.author);
}

function rememberSelection(storage: StorageAccess | undefined, key: string, selection: VersionedSelection) {
  if (precedes(readSelection(storage, key), selection)) {
    save(storage, key, JSON.stringify(selection));
  }
}

/** Share the group cache even if the last active panel was at the other origin. */
export function cacheRelaySelection(storage: StorageAccess | undefined, message: LinkMessage) {
  if (message.group !== "none" && (message.type === "locate" || message.type === "symbol")) {
    rememberSelection(storage, selectionKey(message.group, ""), message.selection);
  }
}

export function cachedRelayReply(storage: StorageAccess | undefined, message: LinkMessage): LinkMessage | undefined {
  if (message.type !== "ask" || message.group === "none") return;
  const selection = readSelection(storage, selectionKey(message.group, ""));
  if (selection.revision === 0) return;
  return { type: "symbol", group: message.group, from: "relay", to: message.from, selection };
}

export function createLink({ key, panel, transport, storage, preferences = storage }: LinkOptions) {
  const from = globalThis.crypto.randomUUID();
  const preferenceKey = `talaria-link-group:${key}`;
  const savedGroup = read(preferences, preferenceKey);
  let group: LinkGroup = isGroup(savedGroup) ? savedGroup : "red";
  const selectionStorage = () => group === "none" ? preferences : storage;
  let selection = readSelection(selectionStorage(), selectionKey(group, key));
  let locateTs: number | null = null;
  let crosshair: number | null = null;
  let crosshairOwner: string | null = null;
  let lastSentCrosshair: number | null = null;
  let peerList: Peer[] = [];
  let joined = false;
  const peers = new Map<string, { peer: Peer; seen: number }>();
  const listeners = new Set<() => void>();
  const snapshot = (): LinkSnapshot => ({
    instrumentId: selection.instrumentId,
    timeframe: selection.timeframe,
    group, locateTs, crosshair, peers: peerList,
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
      peer: { from, group, panel, instrumentId: selection.instrumentId, timeframe: selection.timeframe },
    });
  }

  function setSelection(next: VersionedSelection, ts: number | null) {
    selection = next;
    locateTs = ts;
    crosshair = null;
    crosshairOwner = null;
    lastSentCrosshair = null;
    rememberSelection(selectionStorage(), selectionKey(group, key), selection);
    publish();
  }

  function applySelection(next: Selection, ts: number | null) {
    const saved = readSelection(selectionStorage(), selectionKey(group, key));
    const nextSelection = {
      ...next,
      revision: Math.max(Date.now(), selection.revision + 1, saved.revision + 1),
      author: from,
    };
    setSelection(nextSelection, ts);
    if (group !== "none") {
      if (ts === null) send({ type: "symbol", selection: nextSelection });
      else send({ type: "locate", selection: nextSelection, ts });
    }
    beat();
  }

  function join() {
    joined = true;
    beat();
    if (group !== "none") send({ type: "ask" });
  }

  function leave() {
    if (joined) send({ type: "bye" });
    joined = false;
  }

  const unsubscribe = transport.subscribe((message) => {
    if (!joined || !message || message.from === from) return;
    if (message.type === "presence") {
      const previous = peers.get(message.from)?.peer;
      peers.set(message.from, { peer: message.peer, seen: Date.now() });
      if (
        !previous || previous.group !== message.peer.group ||
        previous.instrumentId !== message.peer.instrumentId || previous.timeframe !== message.peer.timeframe
      ) {
        updatePeers();
      }
      return;
    }
    if (message.type === "bye") {
      if (peers.delete(message.from)) updatePeers();
      if (message.from === crosshairOwner) {
        crosshair = null;
        crosshairOwner = null;
        publish();
      }
      return;
    }
    if (group === "none" || message.group !== group) return;
    switch (message.type) {
      case "ask":
        send({ type: "symbol", selection, to: message.from });
        return;
      case "symbol":
        if (message.to !== undefined && message.to !== from) return;
        if (precedes(selection, message.selection)) setSelection(message.selection, null);
        return;
      case "locate":
        if (precedes(selection, message.selection)) setSelection(message.selection, message.ts);
        return;
      case "crosshair":
        if (panel !== "chart") return;
        if (message.ts === null && crosshairOwner !== message.from) return;
        crosshairOwner = message.from;
        if (crosshair !== message.ts) {
          crosshair = message.ts;
          publish();
        }
        return;
    }
  });

  join();
  const unsubscribeReady = transport.onReady?.(() => { if (joined) join(); });
  const heartbeat = setInterval(() => {
    if (!joined) return;
    let expired = false;
    for (const [id, { seen }] of peers) {
      if (Date.now() - seen >= PEER_TTL_MS) {
        peers.delete(id);
        expired = true;
        if (crosshairOwner === id) {
          crosshairOwner = null;
          crosshair = null;
        }
      }
    }
    if (expired) updatePeers();
    beat();
  }, HEARTBEAT_MS);

  return {
    getSnapshot: () => current,
    subscribe(listener: () => void) {
      listeners.add(listener);
      return () => { listeners.delete(listener); };
    },
    setGroup(next: LinkGroup) {
      if (next === group) return;
      if (lastSentCrosshair !== null && group !== "none") send({ type: "crosshair", ts: null });
      const previous = selection;
      group = next;
      save(preferences, preferenceKey, group);
      const saved = readSelection(selectionStorage(), selectionKey(group, key));
      setSelection(
        next === "none" && saved.revision === 0
          ? { ...previous, revision: Date.now(), author: from }
          : saved,
        null,
      );
      join();
    },
    setSymbol(instrumentId: string, timeframe: string) {
      applySelection({ instrumentId, timeframe }, null);
    },
    locate(target: LocateTarget) {
      applySelection(
        { instrumentId: target.instrument_id, timeframe: target.timeframe },
        target.triggered_ts > 0 ? target.triggered_ts : null,
      );
    },
    clearLocate() {
      locateTs = null;
      publish();
    },
    sendCrosshair(ts: number | null) {
      if (group === "none" || ts === lastSentCrosshair) return;
      lastSentCrosshair = ts;
      send({ type: "crosshair", ts });
    },
    join,
    leave,
    close({ announce = true } = {}) {
      if (announce) leave();
      clearInterval(heartbeat);
      unsubscribeReady?.();
      unsubscribe();
      listeners.clear();
    },
  };
}

export type PanelLink = ReturnType<typeof createLink>;
