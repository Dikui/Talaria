import { useCallback, useEffect, useRef, useState, type RefObject } from "react";
import type { IndicatorName, IndicatorSeries, KeyLevel, KeyLevelSet } from "@talaria/contracts";

import type { ChartSurfaceAdapter } from "../../chart/adapter";
import { api } from "../../lib/api";
import { timeframeToMs } from "../../lib/chartData";
import type { LinkGroup } from "../../workbench/link";
import { indicatorRequests } from "../indicators/config";
import type { IndicatorSettings } from "../indicators/config";
import {
  computeDeepenIndicatorLimit,
  needsIndicatorDeepenRefetch,
} from "./indicatorBackfill";
import {
  KeyLevelAsOfCache,
  shouldFetchReplayKeyLevels,
  type ReplayKeyLevelTrigger,
} from "./keyLevelCache";
import {
  closedBarCutoff,
  computeNextCursorTime,
  isBarClosedAt,
  openReplaySyncTransport,
  type ReplayPeerNext,
  type ReplaySyncMessage,
  type ReplaySyncTransport,
} from "./sync";
import { createReplayTickSource } from "./tickSource";

export interface UseChartReplayOptions {
  paneId: string;
  instrumentId: string;
  timeframe: string;
  group: LinkGroup;
  adapterRef: RefObject<ChartSurfaceAdapter | null>;
  indicatorSettings: IndicatorSettings;
  indicatorSeries: IndicatorSeries[];
  onIndicatorSeriesBackfilled: (series: IndicatorSeries[]) => void;
  onReplayStateChange?: (state: {
    active: boolean;
    playing: boolean;
    cursorTime: number | null;
    showAllDrawings: boolean;
  }) => void;
}

export interface ChartReplayController {
  active: boolean;
  playing: boolean;
  isMaster: boolean;
  cursorTime: number | null;
  remaining: number;
  intervalMs: number;
  bounds: { first: number; last: number } | null;
  tickReplayEnabled: boolean;
  showAllDrawings: boolean;
  underflowWarning: boolean;
  replayLevels: KeyLevel[] | null;
  replayLevelNotes: string[];
  startAt: (fromBarOpenMs: number) => Promise<void>;
  stepBar: () => void;
  stepTick: () => void;
  play: (intervalMs?: number) => void;
  pause: () => void;
  stop: () => void;
  changeSpeed: (intervalMs: number) => void;
  toggleTickReplay: (enabled: boolean) => void;
  toggleShowAllDrawings: (showAll: boolean) => void;
}

export async function fetchReplayKeyLevelsIfNeeded(params: {
  trigger: ReplayKeyLevelTrigger;
  isPlaying: boolean;
  instrumentId: string;
  timeframe: string;
  asOf: number | null;
  cache: KeyLevelAsOfCache;
  fetchKeyLevelsAt: (opts: {
    instrument: string;
    timeframe: string;
    asOf: number;
  }) => Promise<KeyLevelSet>;
}): Promise<KeyLevelSet | null> {
  if (params.asOf === null || !shouldFetchReplayKeyLevels(params.trigger, params.isPlaying)) {
    return null;
  }
  const cached = params.cache.get(params.instrumentId, params.timeframe, params.asOf);
  if (cached) return cached;
  const fetched = await params.fetchKeyLevelsAt({
    instrument: params.instrumentId,
    timeframe: params.timeframe,
    asOf: params.asOf,
  });
  params.cache.set(params.instrumentId, params.timeframe, params.asOf, fetched);
  return fetched;
}

export function useChartReplay({
  paneId,
  instrumentId,
  timeframe,
  group,
  adapterRef,
  indicatorSettings,
  indicatorSeries,
  onIndicatorSeriesBackfilled,
  onReplayStateChange,
}: UseChartReplayOptions): ChartReplayController {
  const tfMs = timeframeToMs(timeframe);

  const [active, setActive] = useState(false);
  const [playing, setPlaying] = useState(false);
  const [isMaster, setIsMaster] = useState(true);
  const [cursorTime, setCursorTime] = useState<number | null>(null);
  const [remaining, setRemaining] = useState(0);
  const [intervalMs, setIntervalMs] = useState(1000);
  const [bounds, setBounds] = useState<{ first: number; last: number } | null>(null);
  const [tickReplayEnabled, setTickReplayEnabled] = useState(true);
  const [showAllDrawings, setShowAllDrawings] = useState(false);
  const [underflowWarning, setUnderflowWarning] = useState(false);
  const [replayLevels, setReplayLevels] = useState<KeyLevel[] | null>(null);
  const [replayLevelNotes, setReplayLevelNotes] = useState<string[]>([]);

  const transportRef = useRef<ReplaySyncTransport | null>(null);
  const sessionIdRef = useRef<string | null>(null);
  const isMasterRef = useRef(true);
  const cursorTimeRef = useRef<number | null>(null);
  const playingRef = useRef(false);
  const peersRef = useRef<Map<string, ReplayPeerNext>>(new Map());
  const keyLevelCacheRef = useRef<KeyLevelAsOfCache>(new KeyLevelAsOfCache(128));
  const deepenedKeyRef = useRef<string | null>(null);
  const latestRef = useRef({
    paneId,
    instrumentId,
    timeframe,
    tfMs,
    group,
    indicatorSettings,
    indicatorSeries,
    tickReplayEnabled,
    showAllDrawings,
  });
  latestRef.current = {
    paneId,
    instrumentId,
    timeframe,
    tfMs,
    group,
    indicatorSettings,
    indicatorSeries,
    tickReplayEnabled,
    showAllDrawings,
  };

  const refreshBounds = useCallback(() => {
    const adapter = adapterRef.current;
    setBounds(adapter?.replay.bounds ?? null);
  }, [adapterRef]);

  const applyTickSource = useCallback(
    (enabled: boolean, asOf: number | null) => {
      const adapter = adapterRef.current;
      if (!adapter) return;
      if (!enabled) {
        adapter.replay.setTicks(null);
        return;
      }
      adapter.replay.setTicks(
        createReplayTickSource({
          instrumentId: latestRef.current.instrumentId,
          asOf,
          limit: 2000,
          fetchTicks: api.ticks,
        }),
      );
    },
    [adapterRef],
  );

  const maybeRefetchDeepenedIndicators = useCallback(
    async (asOfCursor: number) => {
      const adapter = adapterRef.current;
      if (!adapter) return;
      const oldestBar = adapter.getOldestLoadedBarTime();
      const {
        instrumentId: inst,
        timeframe: tf,
        tfMs: periodMs,
        indicatorSettings: settings,
        indicatorSeries: currentSeries,
      } = latestRef.current;

      if (!needsIndicatorDeepenRefetch(oldestBar, currentSeries) || oldestBar === null) {
        return;
      }
      const dedupKey = `${inst}:${tf}:${oldestBar}`;
      if (deepenedKeyRef.current === dedupKey) return;
      deepenedKeyRef.current = dedupKey;

      const enabledNames = indicatorRequests(settings, "replay", tf, 300).map(
        (req) => req.operator as IndicatorName,
      );
      if (enabledNames.length === 0) return;

      const newestBar = adapter.replay.bounds?.last ?? asOfCursor;
      const limit = computeDeepenIndicatorLimit(oldestBar, newestBar, periodMs);
      try {
        const bundle = await api.indicatorsAt({
          instrumentId: inst,
          timeframe: tf,
          names: enabledNames,
          asOf: asOfCursor,
          limit,
        });
        adapter.setIndicators(bundle.series);
        onIndicatorSeriesBackfilled(bundle.series);
      } catch {
        // Keep best-effort indicators; allow retry on next deepen.
        deepenedKeyRef.current = null;
      }
    },
    [adapterRef, onIndicatorSeriesBackfilled],
  );

  const maybeUpdateKeyLevels = useCallback(
    async (trigger: ReplayKeyLevelTrigger, asOf: number | null, nowPlaying: boolean) => {
      const { instrumentId: inst, timeframe: tf } = latestRef.current;
      try {
        const result = await fetchReplayKeyLevelsIfNeeded({
          trigger,
          isPlaying: nowPlaying,
          instrumentId: inst,
          timeframe: tf,
          asOf,
          cache: keyLevelCacheRef.current,
          fetchKeyLevelsAt: api.keyLevelsAt,
        });
        if (result) {
          setReplayLevels(result.levels ?? []);
          setReplayLevelNotes(result.notes ?? []);
        }
      } catch {
        // Ignore transient key-level calculation failures during replay.
      }
    },
    [],
  );

  const broadcast = useCallback((msg: ReplaySyncMessage) => {
    if (latestRef.current.group === "none") return;
    transportRef.current?.send(msg);
  }, []);

  // Open BroadcastChannel transport once.
  useEffect(() => {
    const transport = openReplaySyncTransport();
    transportRef.current = transport;

    const unsub = transport.subscribe((msg) => {
      const {
        paneId: selfPane,
        group: selfGroup,
        tfMs: selfTfMs,
        instrumentId: selfInst,
      } = latestRef.current;
      if (selfGroup === "none" || msg.group !== selfGroup) return;

      const adapter = adapterRef.current;
      if (!adapter) return;

      if (msg.type === "replay:peer-next") {
        if (isMasterRef.current && msg.sessionId === sessionIdRef.current) {
          peersRef.current.set(msg.paneId, {
            paneId: msg.paneId,
            tfMs: msg.tfMs,
            nextTime: msg.nextTime,
          });
        }
        return;
      }

      if (msg.masterPaneId === selfPane) return;
      if (msg.type === "replay:sync-start") {
        if (msg.instrumentId !== selfInst) return;
        sessionIdRef.current = msg.sessionId;
        isMasterRef.current = false;
        setIsMaster(false);
        cursorTimeRef.current = msg.cursorTime;
        setCursorTime(msg.cursorTime);
        setShowAllDrawings(msg.showAllDrawings);
        setActive(true);
        const targetOpen = closedBarCutoff(msg.cursorTime, selfTfMs);
        void adapter.replay.start({ from: targetOpen }).then(() => {
          const b = adapter.replay.bounds;
          setBounds(b);
          setUnderflowWarning(b !== null && !isBarClosedAt(b.first, selfTfMs, msg.cursorTime));
          setRemaining(adapter.replay.state.remaining);
          broadcast({
            type: "replay:peer-next",
            sessionId: msg.sessionId,
            paneId: selfPane,
            group: selfGroup,
            tfMs: selfTfMs,
            nextTime: adapter.replay.state.nextTime,
          });
        });
        return;
      }

      if (msg.sessionId !== sessionIdRef.current) return;

      if (msg.type === "replay:sync-cursor") {
        cursorTimeRef.current = msg.cursorTime;
        setCursorTime(msg.cursorTime);
        const state = adapter.replay.state;
        const targetOpen = closedBarCutoff(msg.cursorTime, selfTfMs);
        if (state.cursorTime !== null && state.cursorTime > targetOpen) {
          void adapter.replay.start({ from: targetOpen }).then(() => {
            const b = adapter.replay.bounds;
            setUnderflowWarning(b !== null && !isBarClosedAt(b.first, selfTfMs, msg.cursorTime));
            setRemaining(adapter.replay.state.remaining);
          });
        } else {
          let safety = 0;
          while (safety < 500) {
            const next = adapter.replay.state.nextTime;
            if (next === null || !isBarClosedAt(next, selfTfMs, msg.cursorTime)) break;
            if (!adapter.replay.step()) break;
            safety += 1;
          }
          const b = adapter.replay.bounds;
          setUnderflowWarning(b !== null && !isBarClosedAt(b.first, selfTfMs, msg.cursorTime));
          setRemaining(adapter.replay.state.remaining);
        }
        broadcast({
          type: "replay:peer-next",
          sessionId: msg.sessionId,
          paneId: selfPane,
          group: selfGroup,
          tfMs: selfTfMs,
          nextTime: adapter.replay.state.nextTime,
        });
        return;
      }

      if (msg.type === "replay:sync-play") {
        playingRef.current = msg.playing;
        setPlaying(msg.playing);
        setIntervalMs(msg.intervalMs);
        if (!msg.playing) {
          void maybeUpdateKeyLevels("pause", cursorTimeRef.current, false);
        }
        return;
      }

      if (msg.type === "replay:sync-drawings") {
        setShowAllDrawings(msg.showAllDrawings);
        return;
      }

      if (msg.type === "replay:sync-stop") {
        sessionIdRef.current = null;
        isMasterRef.current = true;
        setIsMaster(true);
        playingRef.current = false;
        setPlaying(false);
        setActive(false);
        setCursorTime(null);
        cursorTimeRef.current = null;
        setUnderflowWarning(false);
        setReplayLevels(null);
        setReplayLevelNotes([]);
        adapter.replay.stop();
      }
    });

    return () => {
      unsub();
      transport.close();
      transportRef.current = null;
    };
  }, [adapterRef, broadcast, maybeUpdateKeyLevels]);

  // Subscribe to Vela replay:* events on the local chart adapter.
  useEffect(() => {
    const adapter = adapterRef.current;
    if (!adapter) return;

    refreshBounds();
    const unsubs = [
      adapter.replay.on("replay:start", (ev) => {
        const { tfMs: periodMs, tickReplayEnabled: ticksOn } = latestRef.current;
        const effectiveCursor = isMasterRef.current
          ? ev.cursorTime + periodMs
          : (cursorTimeRef.current ?? ev.cursorTime + periodMs);
        cursorTimeRef.current = effectiveCursor;
        setActive(true);
        setPlaying(false);
        playingRef.current = false;
        setCursorTime(effectiveCursor);
        setRemaining(ev.remaining);
        setBounds(adapter.replay.bounds);
        applyTickSource(ticksOn, effectiveCursor);
        if (isMasterRef.current && sessionIdRef.current) {
          peersRef.current.set(latestRef.current.paneId, {
            paneId: latestRef.current.paneId,
            tfMs: periodMs,
            nextTime: adapter.replay.state.nextTime,
          });
        }
        void maybeRefetchDeepenedIndicators(effectiveCursor);
        void maybeUpdateKeyLevels("start", effectiveCursor, false);
      }),
      adapter.replay.on("replay:step", (ev) => {
        const { tfMs: periodMs, paneId: selfPane, group: selfGroup } = latestRef.current;
        setRemaining(ev.remaining);
        if (isMasterRef.current) {
          const nextCursor = ev.cursorTime + periodMs;
          cursorTimeRef.current = nextCursor;
          setCursorTime(nextCursor);
          peersRef.current.set(selfPane, {
            paneId: selfPane,
            tfMs: periodMs,
            nextTime: adapter.replay.state.nextTime,
          });
          if (sessionIdRef.current) {
            broadcast({
              type: "replay:sync-cursor",
              sessionId: sessionIdRef.current,
              masterPaneId: selfPane,
              group: selfGroup,
              cursorTime: nextCursor,
            });
          }
          void maybeUpdateKeyLevels("step", nextCursor, playingRef.current);
        }
      }),
      adapter.replay.on("replay:play", (ev) => {
        playingRef.current = true;
        setPlaying(true);
        setIntervalMs(ev.intervalMs);
      }),
      adapter.replay.on("replay:pause", () => {
        playingRef.current = false;
        setPlaying(false);
        void maybeUpdateKeyLevels("pause", cursorTimeRef.current, false);
      }),
      adapter.replay.on("replay:end", () => {
        playingRef.current = false;
        setPlaying(false);
        setActive(false);
        setCursorTime(null);
        cursorTimeRef.current = null;
        setRemaining(0);
        setUnderflowWarning(false);
        setReplayLevels(null);
        setReplayLevelNotes([]);
        adapter.replay.setTicks(null);
      }),
    ];
    return () => {
      for (const unsub of unsubs) unsub();
    };
  }, [
    adapterRef,
    applyTickSource,
    broadcast,
    maybeRefetchDeepenedIndicators,
    maybeUpdateKeyLevels,
    refreshBounds,
  ]);

  // Keep drawing read-only filter & Workbench state synced.
  useEffect(() => {
    adapterRef.current?.setReplayDrawingFilter({
      active,
      cursorTime,
      showAll: showAllDrawings,
    });
    onReplayStateChange?.({
      active,
      playing,
      cursorTime,
      showAllDrawings,
    });
  }, [active, playing, cursorTime, showAllDrawings, adapterRef, onReplayStateChange]);

  // Keep bounds refreshed on market changes.
  useEffect(() => {
    refreshBounds();
  }, [instrumentId, timeframe, refreshBounds]);

  const startAt = useCallback(
    async (fromBarOpenMs: number) => {
      const adapter = adapterRef.current;
      if (!adapter) return;
      const {
        paneId: selfPane,
        instrumentId: inst,
        tfMs: periodMs,
        group: selfGroup,
        showAllDrawings: showAll,
      } = latestRef.current;
      const sessionId =
        typeof globalThis.crypto?.randomUUID === "function"
          ? globalThis.crypto.randomUUID()
          : `replay-${Date.now()}`;
      sessionIdRef.current = sessionId;
      isMasterRef.current = true;
      setIsMaster(true);
      peersRef.current.clear();

      const nextCursor = fromBarOpenMs + periodMs;
      cursorTimeRef.current = nextCursor;
      await adapter.replay.start({ from: fromBarOpenMs });
      const actualCursor =
        adapter.replay.state.cursorTime !== null
          ? adapter.replay.state.cursorTime + periodMs
          : nextCursor;
      cursorTimeRef.current = actualCursor;
      setCursorTime(actualCursor);

      broadcast({
        type: "replay:sync-start",
        sessionId,
        masterPaneId: selfPane,
        group: selfGroup,
        instrumentId: inst,
        cursorTime: actualCursor,
        showAllDrawings: showAll,
      });
    },
    [adapterRef, broadcast],
  );

  const stepBar = useCallback(() => {
    const adapter = adapterRef.current;
    if (!adapter || !isMasterRef.current) return;
    const peers = [...peersRef.current.values()];
    const minTarget = computeNextCursorTime(peers, cursorTimeRef.current);
    const selfNext = adapter.replay.state.nextTime;
    if (
      minTarget !== null &&
      selfNext !== null &&
      selfNext + latestRef.current.tfMs > minTarget &&
      sessionIdRef.current
    ) {
      cursorTimeRef.current = minTarget;
      setCursorTime(minTarget);
      broadcast({
        type: "replay:sync-cursor",
        sessionId: sessionIdRef.current,
        masterPaneId: latestRef.current.paneId,
        group: latestRef.current.group,
        cursorTime: minTarget,
      });
      void maybeUpdateKeyLevels("step", minTarget, false);
      return;
    }
    adapter.replay.step();
  }, [adapterRef, broadcast, maybeUpdateKeyLevels]);

  const stepTick = useCallback(() => {
    const adapter = adapterRef.current;
    if (!adapter || !isMasterRef.current) return;
    adapter.replay.stepUpdate();
  }, [adapterRef]);

  const play = useCallback(
    (nextIntervalMs?: number) => {
      const adapter = adapterRef.current;
      if (!adapter || !isMasterRef.current) return;
      const pace = nextIntervalMs ?? intervalMs;
      playingRef.current = true;
      setPlaying(true);
      setIntervalMs(pace);
      adapter.replay.play(pace);
      if (sessionIdRef.current) {
        broadcast({
          type: "replay:sync-play",
          sessionId: sessionIdRef.current,
          masterPaneId: latestRef.current.paneId,
          group: latestRef.current.group,
          playing: true,
          intervalMs: pace,
        });
      }
    },
    [adapterRef, broadcast, intervalMs],
  );

  const pause = useCallback(() => {
    const adapter = adapterRef.current;
    if (!adapter || !isMasterRef.current) return;
    playingRef.current = false;
    setPlaying(false);
    adapter.replay.pause();
    if (sessionIdRef.current) {
      broadcast({
        type: "replay:sync-play",
        sessionId: sessionIdRef.current,
        masterPaneId: latestRef.current.paneId,
        group: latestRef.current.group,
        playing: false,
        intervalMs,
      });
    }
  }, [adapterRef, broadcast, intervalMs]);

  const stop = useCallback(() => {
    const adapter = adapterRef.current;
    const sessionId = sessionIdRef.current;
    sessionIdRef.current = null;
    isMasterRef.current = true;
    setIsMaster(true);
    playingRef.current = false;
    setPlaying(false);
    setActive(false);
    setCursorTime(null);
    cursorTimeRef.current = null;
    setUnderflowWarning(false);
    setReplayLevels(null);
    setReplayLevelNotes([]);
    adapter?.replay.stop();
    if (sessionId) {
      broadcast({
        type: "replay:sync-stop",
        sessionId,
        masterPaneId: latestRef.current.paneId,
        group: latestRef.current.group,
      });
    }
  }, [adapterRef, broadcast]);

  const changeSpeed = useCallback(
    (nextIntervalMs: number) => {
      setIntervalMs(nextIntervalMs);
      if (playingRef.current) {
        play(nextIntervalMs);
      }
    },
    [play],
  );

  const toggleTickReplay = useCallback(
    (enabled: boolean) => {
      setTickReplayEnabled(enabled);
      if (active) {
        applyTickSource(enabled, cursorTimeRef.current);
      }
    },
    [active, applyTickSource],
  );

  const toggleShowAllDrawings = useCallback(
    (showAll: boolean) => {
      setShowAllDrawings(showAll);
      if (sessionIdRef.current && isMasterRef.current) {
        broadcast({
          type: "replay:sync-drawings",
          sessionId: sessionIdRef.current,
          masterPaneId: latestRef.current.paneId,
          group: latestRef.current.group,
          showAllDrawings: showAll,
        });
      }
    },
    [broadcast],
  );

  return {
    active,
    playing,
    isMaster,
    cursorTime,
    remaining,
    intervalMs,
    bounds,
    tickReplayEnabled,
    showAllDrawings,
    underflowWarning,
    replayLevels,
    replayLevelNotes,
    startAt,
    stepBar,
    stepTick,
    play,
    pause,
    stop,
    changeSpeed,
    toggleTickReplay,
    toggleShowAllDrawings,
  };
}
