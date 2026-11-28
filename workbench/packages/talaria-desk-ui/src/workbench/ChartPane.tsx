import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ChevronDown, ExternalLink, RefreshCw, Search, X } from "lucide-react";
import type {
  AgentAnnotationRecord,
  DerivativeBundle,
  DraftRecord,
  IndicatorBundle,
  KeyLevel,
  PaperAccountView,
  PaperRound,
  SizingPreviewResponse,
  SnapshotMeta,
  StructureFeature,
} from "@talaria/contracts";

import {
  VELA_ATTRIBUTION,
  type BarrierDragDisabledEvent,
  type BarrierDragOverlay,
  type BarrierDragProposal,
  type ChartStats,
  type OrderGestureCreateDraftPayload,
  type OrderGestureGeometry,
  type RenderBackendMode,
} from "../chart/adapter";
import { VelaChartAdapter } from "../chart/VelaChartAdapter";
import { isNonTerminalDraft } from "../chart/velaLayers";
import { DerivativeSummary } from "../features/derivatives/DerivativeSummary";
import { FeedAlert, FeedHealthBadge, OrderBookSummary } from "../features/feed/FeedIndicators";
import { deskSocketHub } from "../features/feed/deskSocketHub";
import { feedBadgeView } from "../features/feed/health";
import { useMarketStream } from "../features/feed/useMarketStream";
import { IndicatorToolbar } from "../features/indicators/IndicatorToolbar";
import { indicatorRequests } from "../features/indicators/config";
import {
  formatLevelPrice,
  isKeyLevelsEnabled,
  keyLevelKindLabel,
  toggleKeyLevelsEnabled,
  visibleKeyLevels,
} from "../features/key-levels/model";
import {
  detectorLabel,
  formatStructureGeometry,
  isStructuresEnabled,
  structureMidPrice,
  toggleStructuresEnabled,
  visibleStructures,
} from "../features/structures/model";
import { annotationPriceLabel } from "../features/annotations/model";
import { BarrierDragConfirmModal } from "../features/paper/BarrierDragConfirmModal";
import {
  buildDraggableBarriers,
  useBarrierDragCommands,
} from "../features/paper/useBarrierDragCommands";
import {
  resolveDefaultSubAccount,
  useLastSubAccount,
} from "../features/paper/useLastSubAccount";
import {
  activePaperBarriersForInstrument,
  paperViewsToTradeExecutions,
} from "../features/paper/usePaperAccounts";
import "../features/paper/paper.css";
import { ReplayToolbar } from "../features/replay/ReplayToolbar";
import { filterReplayAnnotations } from "../features/replay/filters";
import { useChartReplay } from "../features/replay/useChartReplay";
import { roundsToTradeExecutions, runRoundToBarrier } from "../features/runs/model";
import { api } from "../lib/api";
import { timeframeToMs } from "../lib/chartData";
import { t, useLocale } from "../lib/i18n";
import { TIMEFRAMES } from "./panels";
import { useLinkCrosshair, useLinkSnapshot, usePanelLink } from "./usePanelLink";
import {
  groupInstrumentsByVenue,
  symbolDisplayName,
  venueShortLabel,
} from "./venues";
import { useWorkbenchContext } from "./WorkbenchContext";

export interface ChartPaneProps {
  paneId: string;
  initialTimeframe?: string;
  syncTimeframe?: boolean;
  isPopout?: boolean;
  onTitleChange?: (title: string) => void;
}

const INDICATOR_PAGE_SIZE = 300;

export function ChartPane({
  paneId,
  initialTimeframe = "1m",
  syncTimeframe = false,
  isPopout = false,
  onTitleChange,
}: ChartPaneProps) {
  const locale = useLocale();
  const ctx = useWorkbenchContext();
  const link = usePanelLink({
    key: paneId,
    panel: "chart",
    initialTimeframe,
    syncTimeframe,
  });
  const { instrumentId, timeframe, locateTs: snapshotCutoff, group } = useLinkSnapshot(link);
  const externalCrosshair = useLinkCrosshair(link);
  const stream = useMarketStream(instrumentId, timeframe);

  const containerRef = useRef<HTMLDivElement | null>(null);
  const adapterRef = useRef<VelaChartAdapter | null>(null);
  const marketRef = useRef({ instrumentId, timeframe });
  marketRef.current = { instrumentId, timeframe };

  const [snapshot, setSnapshot] = useState<SnapshotMeta | null>(null);
  const [stats, setStats] = useState<ChartStats>({ loaded: 0, gaps: 0 });
  const [backend, setBackend] = useState<RenderBackendMode>("auto");
  const [derivativeBundle, setDerivativeBundle] = useState<DerivativeBundle | null>(null);
  const [indicatorBundle, setIndicatorBundle] = useState<IndicatorBundle | null>(null);
  const [indicatorsLoading, setIndicatorsLoading] = useState(false);
  const [indicatorError, setIndicatorError] = useState<string | null>(null);
  const [barriers, setBarriers] = useState<PaperRound[]>([]);
  const [paperViewsById, setPaperViewsById] = useState<Record<string, PaperAccountView>>({});
  const [paperAccountIds, setPaperAccountIds] = useState<string[]>([]);
  const [stagedDrafts, setStagedDrafts] = useState<DraftRecord[]>([]);
  const [activeOrderTool, setActiveOrderTool] = useState<"LONG" | "SHORT" | null>(null);
  const [orderGesture, setOrderGesture] = useState<OrderGestureGeometry | null>(null);
  const [orderPreview, setOrderPreview] = useState<SizingPreviewResponse | null>(null);
  const [orderPreviewError, setOrderPreviewError] = useState<string | null>(null);
  const [orderPreviewMs, setOrderPreviewMs] = useState<number | null>(null);
  const [creatingDraftBusy, setCreatingDraftBusy] = useState(false);
  const [orderGestureNotice, setOrderGestureNotice] = useState<string | null>(null);
  const [inspectedLevel, setInspectedLevel] = useState<KeyLevel | null>(null);
  const [inspectedStructure, setInspectedStructure] = useState<StructureFeature | null>(null);
  const [inspectedAnnotation, setInspectedAnnotation] =
    useState<AgentAnnotationRecord | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  const { lastSubAccount } = useLastSubAccount(paperAccountIds);
  const effectiveSubAccount = useMemo(
    () => resolveDefaultSubAccount(paperAccountIds, lastSubAccount),
    [paperAccountIds, lastSubAccount],
  );
  const effectiveSubAccountRef = useRef(effectiveSubAccount);
  effectiveSubAccountRef.current = effectiveSubAccount;
  const snapshotRef = useRef<SnapshotMeta | null>(null);
  snapshotRef.current = snapshot;
  const createDraftFromGestureRef = useRef<(payload: OrderGestureCreateDraftPayload) => void>(
    () => undefined,
  );
  const barrierDragEndRef = useRef<(proposal: BarrierDragProposal) => void>(() => undefined);
  const barrierDragDisabledRef = useRef<(ev: BarrierDragDisabledEvent) => void>(() => undefined);
  const barrierPendingAckTimeoutRef = useRef<(ov: BarrierDragOverlay) => void>(() => undefined);

  const cancelBarrierConfirm = useCallback(() => {
    adapterRef.current?.cancelBarrierDragConfirm();
  }, []);
  const markBarrierPendingAck = useCallback(
    (commandId: string, proposal: BarrierDragProposal) => {
      adapterRef.current?.markBarrierDragPendingAck(commandId, proposal);
    },
    [],
  );
  const resolveBarrierPendingAck = useCallback((commandId?: string) => {
    adapterRef.current?.resolveBarrierDragPendingAck(commandId);
  }, []);

  const barrierDrag = useBarrierDragCommands({
    viewsById: paperViewsById,
    onCancelPendingConfirm: cancelBarrierConfirm,
    onMarkPendingAck: markBarrierPendingAck,
    onResolvePendingAck: resolveBarrierPendingAck,
  });
  barrierDragEndRef.current = barrierDrag.handleDragEnd;
  barrierDragDisabledRef.current = barrierDrag.handleDisabledAttempt;
  barrierPendingAckTimeoutRef.current = barrierDrag.handlePendingAckTimeout;

  const tfMs = timeframeToMs(timeframe);
  const { onPaneReplayActiveChange } = ctx;

  const replay = useChartReplay({
    paneId,
    instrumentId,
    timeframe,
    group,
    adapterRef,
    indicatorSettings: ctx.indicatorSettings,
    indicatorSeries: indicatorBundle?.series ?? [],
    onIndicatorSeriesBackfilled: (series) => {
      setIndicatorBundle({
        snapshot_id: snapshot?.snapshot_id ?? "replay",
        instrument_id: instrumentId,
        timeframe,
        series,
      });
    },
  });

  useEffect(() => {
    onPaneReplayActiveChange(paneId, replay.active);
    return () => {
      onPaneReplayActiveChange(paneId, false);
    };
  }, [paneId, replay.active, onPaneReplayActiveChange]);

  const venueGroups = useMemo(
    () => groupInstrumentsByVenue(ctx.instruments),
    [ctx.instruments],
  );
  const spec = useMemo(
    () => ctx.instruments.find((item) => item.instrument_id === instrumentId),
    [ctx.instruments, instrumentId],
  );
  const activeVenue = spec?.venue ?? venueGroups[0]?.venue ?? "";
  const pricePrecision = spec?.price_precision ?? 2;
  const volumePrecision = spec?.qty_precision ?? 3;

  useEffect(() => {
    const title =
      venueGroups.length > 1 && spec
        ? `${venueShortLabel(spec.venue)} · ${symbolDisplayName(spec)} · ${timeframe}`
        : `${instrumentId} · ${timeframe}`;
    onTitleChange?.(title);
  }, [venueGroups.length, spec, instrumentId, timeframe, onTitleChange]);

  // Mount VelaChartAdapter once per ChartPane lifecycle.
  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;

    const adapter = new VelaChartAdapter();
    adapterRef.current = adapter;
    adapter.mount(el, {
      chartId: paneId,
      instrumentId,
      timeframe,
      pricePrecision,
      volumePrecision,
      theme: ctx.theme,
      locale: ctx.locale,
      readOnly: ctx.readOnly,
      live: true,
      snapshotCutoff,
      onStats: (nextStats) => setStats(nextStats),
      onSnapshot: (meta) => setSnapshot(meta),
      onBackendChange: (mode) => setBackend(mode),
      onCrosshairMove: ({ time, price }) => {
        link.sendCrosshair(time, price);
      },
      onSelectKeyLevel: (level) => {
        setInspectedAnnotation(null);
        setInspectedStructure(null);
        setInspectedLevel(level);
      },
      onSelectStructure: (structure) => {
        setInspectedAnnotation(null);
        setInspectedLevel(null);
        setInspectedStructure(structure);
      },
      onSelectAnnotation: (annotation) => {
        setInspectedLevel(null);
        setInspectedStructure(null);
        setInspectedAnnotation(annotation);
      },
      onSelectStagedDraft: (draft) => {
        setInspectedLevel(null);
        setInspectedAnnotation(null);
        ctx.onSelectDraft(draft.draft_id);
        ctx.onOpenRightTab("drafts");
      },
      onOrderGestureChange: (gesture) => {
        setOrderGesture(gesture);
        setOrderGestureNotice(null);
        if (!gesture) {
          setOrderPreview(null);
          setOrderPreviewError(null);
          setOrderPreviewMs(null);
        }
      },
      onCreateDraftFromOrderGesture: (payload) => {
        createDraftFromGestureRef.current(payload);
      },
      onDrawingToolChange: (tool) => {
        if (tool !== "position") {
          setActiveOrderTool(null);
        }
      },
      onBarrierDragEnd: (proposal) => {
        barrierDragEndRef.current(proposal);
      },
      onBarrierDragDisabled: (ev) => {
        barrierDragDisabledRef.current(ev);
      },
      onBarrierPendingAckTimeout: (ov) => {
        barrierPendingAckTimeoutRef.current(ov);
      },
      onCreateAlertFromDrawing: ({ drawingId, price }) => {
        ctx.onSeedAlert({
          drawingId,
          price,
          instrumentId: marketRef.current.instrumentId,
          timeframe: marketRef.current.timeframe,
        });
        ctx.onOpenRightTab("alerts");
      },
      onDrawingsChanged: (records, source) => {
        ctx.onDrawingsUpdated(records);
        if (source === "mutation") {
          link.notifyDrawingsChanged(marketRef.current.instrumentId);
        }
      },
      onDrawingSelect: (drawingId) => {
        ctx.onDrawingSelected(drawingId);
      },
      onLoadError: (err) => {
        setLoadError(err instanceof Error ? err.message : t("加载行情失败", "Failed to load bars"));
      },
    });

    const resizeObserver = new ResizeObserver(() => {
      adapter.resize();
    });
    resizeObserver.observe(el);

    return () => {
      resizeObserver.disconnect();
      adapter.destroy();
      adapterRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [paneId]);

  // Sync market switches cleanly in place.
  const prevMarketRef = useRef({ instrumentId, timeframe, snapshotCutoff });
  useEffect(() => {
    const prev = prevMarketRef.current;
    if (
      prev.instrumentId === instrumentId &&
      prev.timeframe === timeframe &&
      prev.snapshotCutoff === snapshotCutoff
    ) {
      return;
    }
    prevMarketRef.current = { instrumentId, timeframe, snapshotCutoff };
    setLoadError(null);
    setInspectedLevel(null);
    setInspectedStructure(null);
    setInspectedAnnotation(null);
    setActiveOrderTool(null);
    setOrderGesture(null);
    setOrderPreview(null);
    setOrderPreviewError(null);
    setOrderGestureNotice(null);
    void adapterRef.current?.setMarket({
      instrumentId,
      timeframe,
      pricePrecision,
      volumePrecision,
      live: snapshotCutoff === null,
      snapshotCutoff,
    });
  }, [instrumentId, timeframe, snapshotCutoff, pricePrecision, volumePrecision]);

  // Sync theme, locale, read-only state.
  useEffect(() => {
    adapterRef.current?.setTheme(ctx.theme);
  }, [ctx.theme]);

  useEffect(() => {
    adapterRef.current?.setLocale(ctx.locale);
  }, [ctx.locale]);

  useEffect(() => {
    adapterRef.current?.setReadOnly(ctx.readOnly);
  }, [ctx.readOnly]);

  useEffect(() => {
    adapterRef.current?.selectHumanDrawing(ctx.selectedDrawingId);
  }, [ctx.selectedDrawingId]);

  // Sync external BroadcastChannel crosshair onto Vela ghost crosshair.
  useEffect(() => {
    adapterRef.current?.setExternalCrosshair(
      externalCrosshair.ts,
      externalCrosshair.price ?? null,
    );
  }, [externalCrosshair]);

  const {
    activePaneId,
    updatePaneTelemetry,
    onDrawingsUpdated,
  } = ctx;

  // Refresh drawings when another pane / popout saves a drawing on this symbol.
  useEffect(() => {
    let cancelled = false;
    const unsub = link.subscribeDrawings(instrumentId, () => {
      void api
        .drawings(instrumentId, timeframe, { all_timeframes: true })
        .then((res) => {
          if (cancelled) return;
          adapterRef.current?.loadHumanDrawings(res.drawings);
          onDrawingsUpdated(res.drawings);
        })
        .catch(() => undefined);
    });
    return () => {
      cancelled = true;
      unsub();
    };
  }, [instrumentId, timeframe, link, onDrawingsUpdated]);

  // Load derivative summary + execution barriers.
  useEffect(() => {
    if (!snapshot) {
      setDerivativeBundle(null);
      return;
    }
    let cancelled = false;
    api
      .derivatives(snapshot.snapshot_id, timeframe)
      .then((bundle) => {
        if (!cancelled) setDerivativeBundle(bundle);
      })
      .catch(() => {
        if (!cancelled) setDerivativeBundle(null);
      });
    return () => {
      cancelled = true;
    };
  }, [snapshot, timeframe]);

  useEffect(() => {
    let cancelled = false;
    api
      .paperBarriers(instrumentId)
      .then((rounds) => {
        if (!cancelled) setBarriers(Array.isArray(rounds) ? rounds : []);
      })
      .catch(() => {
        if (!cancelled) setBarriers([]);
      });
    api
      .paperAccounts()
      .then(async (ids) => {
        if (cancelled || !Array.isArray(ids)) return;
        setPaperAccountIds(ids);
        const loaded = await Promise.all(
          ids.map((id) => api.paperAccount(id).catch(() => null)),
        );
        if (cancelled) return;
        const map: Record<string, PaperAccountView> = {};
        for (const view of loaded) {
          if (view) map[view.sub_account_id] = view;
        }
        setPaperViewsById(map);
      })
      .catch(() => {
        if (!cancelled) setPaperAccountIds([]);
      });
    return () => {
      cancelled = true;
    };
  }, [instrumentId]);

  const refreshStagedDrafts = useCallback(() => {
    api
      .paperDrafts()
      .then((list) => {
        setStagedDrafts(Array.isArray(list) ? list : []);
      })
      .catch(() => {
        setStagedDrafts([]);
      });
  }, []);

  useEffect(() => {
    refreshStagedDrafts();
  }, [instrumentId, ctx.draftsRevision, refreshStagedDrafts]);

  useEffect(() => {
    return deskSocketHub.subscribePaperAccounts((view) => {
      setPaperAccountIds((prev) =>
        prev.includes(view.sub_account_id) ? prev : [...prev, view.sub_account_id],
      );
      setPaperViewsById((prev) => ({ ...prev, [view.sub_account_id]: view }));
      // Re-pull drafts so confirmed drafts that just executed disappear from LAYER_STAGED_DRAFT.
      refreshStagedDrafts();
    });
  }, [refreshStagedDrafts]);

  const activeBarriers = useMemo(() => {
    const views = Object.values(paperViewsById);
    if (views.length > 0) {
      return activePaperBarriersForInstrument(views, instrumentId);
    }
    return barriers;
  }, [paperViewsById, barriers, instrumentId]);

  // Clear temporary order gesture when entering replay mode.
  useEffect(() => {
    if (replay.active) {
      adapterRef.current?.clearOrderGesture();
      setActiveOrderTool(null);
      setOrderGesture(null);
      setOrderPreview(null);
      setOrderPreviewError(null);
      setOrderGestureNotice(null);
    }
  }, [replay.active]);

  // Push non-terminal drafts for this instrument onto read-only LAYER_STAGED_DRAFT (hidden during replay).
  const visibleStagedDrafts = useMemo(() => {
    if (replay.active || snapshotCutoff !== null) return [];
    return stagedDrafts.filter(
      (draft) => draft.content.instrument_id === instrumentId && isNonTerminalDraft(draft),
    );
  }, [stagedDrafts, instrumentId, replay.active, snapshotCutoff]);

  useEffect(() => {
    adapterRef.current?.setStagedDrafts(visibleStagedDrafts, {
      precision: pricePrecision,
      highlightId: ctx.selectedDraftId,
    });
  }, [visibleStagedDrafts, pricePrecision, ctx.selectedDraftId]);

  // Throttled live sizing preview while placing or dragging temporary 'position' order gesture.
  useEffect(() => {
    if (!orderGesture || replay.active || ctx.readOnly) {
      setOrderPreview(null);
      setOrderPreviewError(null);
      setOrderPreviewMs(null);
      return;
    }
    if (!orderGesture.validGeometry || !effectiveSubAccount) {
      setOrderPreview(null);
      setOrderPreviewError(null);
      return;
    }
    let cancelled = false;
    const timer = window.setTimeout(() => {
      const startedAt = typeof performance !== "undefined" ? performance.now() : Date.now();
      void api
        .previewPaperSizing({
          sub_account_id: effectiveSubAccount,
          instrument_id: instrumentId,
          direction: orderGesture.direction,
          entry_low: orderGesture.entryStr,
          entry_high: orderGesture.entryStr,
          invalidation_stop: orderGesture.stopStr,
          take_profit: [orderGesture.targetStr],
        })
        .then((res) => {
          if (cancelled) return;
          const elapsed =
            (typeof performance !== "undefined" ? performance.now() : Date.now()) - startedAt;
          setOrderPreview(res);
          setOrderPreviewError(null);
          setOrderPreviewMs(Number(elapsed.toFixed(2)));
        })
        .catch((err) => {
          if (cancelled) return;
          setOrderPreview(null);
          setOrderPreviewError(
            err instanceof Error
              ? err.message
              : t("定仓预演不可用", "Sizing preview unavailable"),
          );
        });
    }, 30);

    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [
    orderGesture?.direction,
    orderGesture?.entryStr,
    orderGesture?.stopStr,
    orderGesture?.targetStr,
    orderGesture?.validGeometry,
    effectiveSubAccount,
    instrumentId,
    replay.active,
    ctx.readOnly,
  ]);

  const handleCreateDraftFromGesture = useCallback(
    async (payload: OrderGestureCreateDraftPayload) => {
      if (ctx.readOnly || replay.active || creatingDraftBusy) return;
      if (!payload.validGeometry) {
        setOrderGestureNotice(
          t(
            "止损与止盈须严格位于入场价两侧后才能生成草案。",
            "Stop and take profit must stay strictly on opposite sides of entry.",
          ),
        );
        return;
      }
      setCreatingDraftBusy(true);
      setOrderGestureNotice(null);
      try {
        const currentMarket = marketRef.current;
        let snapshotId = snapshotRef.current?.snapshot_id ?? "";
        if (!snapshotId) {
          const frozen = await api.createSnapshot(currentMarket.instrumentId, [
            currentMarket.timeframe,
          ]);
          snapshotId = frozen.snapshot_id;
        }
        const subAccountId = effectiveSubAccountRef.current || undefined;
        adapterRef.current?.clearOrderGesture();
        setActiveOrderTool(null);
        setOrderGesture(null);
        setOrderPreview(null);
        setOrderPreviewError(null);
        ctx.onSeedDraft({
          instrument_id: currentMarket.instrumentId,
          snapshot_id: snapshotId,
          sub_account_id: subAccountId,
          direction: payload.direction,
          entry_low: payload.entry,
          entry_high: payload.entry,
          invalidation_stop: payload.stop,
          take_profit: [payload.target],
        });
        ctx.onOpenRightTab("drafts");
      } catch (err) {
        setOrderGestureNotice(
          err instanceof Error
            ? err.message
            : t("冻结快照失败，无法生成草案。", "Failed to freeze snapshot for draft."),
        );
      } finally {
        setCreatingDraftBusy(false);
      }
    },
    [ctx, replay.active, creatingDraftBusy],
  );
  createDraftFromGestureRef.current = (payload) => {
    void handleCreateDraftFromGesture(payload);
  };

  useEffect(() => {
    if (ctx.selectedRunRound && ctx.selectedRunRound.instrument_id === instrumentId) {
      adapterRef.current?.setExecutionBarriers([
        {
          ...runRoundToBarrier(ctx.selectedRunRound),
          draggable: false,
          disabled_reason: "READ_ONLY",
        },
      ]);
      return;
    }
    if (replay.active || snapshotCutoff !== null) {
      adapterRef.current?.setExecutionBarriers([]);
      return;
    }
    adapterRef.current?.setExecutionBarriers(
      buildDraggableBarriers(activeBarriers, paperViewsById, {
        readOnly: ctx.readOnly,
        replayActive: replay.active,
      }),
    );
  }, [activeBarriers, paperViewsById, snapshotCutoff, replay.active, ctx.readOnly, ctx.selectedRunRound, instrumentId]);

  const runTradeMarkers = useMemo(
    () => roundsToTradeExecutions(ctx.runsController.rounds, tfMs, instrumentId),
    [ctx.runsController.rounds, tfMs, instrumentId],
  );

  const paperTradeMarkers = useMemo(
    () => paperViewsToTradeExecutions(Object.values(paperViewsById), tfMs, instrumentId),
    [paperViewsById, tfMs, instrumentId],
  );

  useEffect(() => {
    if (replay.active) {
      adapterRef.current?.setTradeMarkers(runTradeMarkers);
      return;
    }
    if (runTradeMarkers.length > 0) {
      adapterRef.current?.setTradeMarkers(runTradeMarkers);
      return;
    }
    adapterRef.current?.setTradeMarkers(snapshotCutoff === null ? paperTradeMarkers : []);
  }, [runTradeMarkers, paperTradeMarkers, replay.active, snapshotCutoff]);

  useEffect(() => {
    const req = ctx.jumpRequest;
    if (!req || req.instrumentId !== instrumentId) return;
    if (ctx.activePaneId !== paneId && !isPopout) return;
    const from = req.openedTs - 30 * tfMs;
    const to = req.closedTs
      ? Math.max(req.openedTs + 40 * tfMs, req.closedTs + 15 * tfMs)
      : req.openedTs + 50 * tfMs;
    adapterRef.current?.setVisibleRange({ from, to });
    ctx.clearJumpRequest(req.nonce);
  }, [ctx.jumpRequest, ctx.activePaneId, ctx.clearJumpRequest, instrumentId, paneId, isPopout, tfMs]);

  useEffect(() => {
    const req = ctx.replayRequest;
    if (!req || req.instrumentId !== instrumentId) return;
    if (ctx.activePaneId !== paneId && !isPopout) return;
    if (req.timeframe && req.timeframe !== timeframe) return;
    void replay.startAt(req.fromTs);
    ctx.clearReplayRequest(req.nonce);
  }, [ctx.replayRequest, ctx.activePaneId, ctx.clearReplayRequest, instrumentId, paneId, isPopout, timeframe, replay]);

  // Fetch backend authoritative indicators (zero frontend math).
  const latestLive = snapshotCutoff === null ? stream.bars.at(-1) : undefined;
  const liveTick =
    latestLive === undefined
      ? ""
      : `${latestLive.bar.timestamp}:${latestLive.revision}:${latestLive.confirmed}`;

  useEffect(() => {
    if (!snapshot) {
      setIndicatorBundle(null);
      adapterRef.current?.setIndicators([]);
      return;
    }
    const requests = indicatorRequests(
      ctx.indicatorSettings,
      snapshot.snapshot_id,
      timeframe,
      INDICATOR_PAGE_SIZE,
    );
    if (requests.length === 0) {
      setIndicatorBundle(null);
      adapterRef.current?.setIndicators([]);
      return;
    }
    let cancelled = false;
    const timer = window.setTimeout(() => {
      setIndicatorsLoading(true);
      void (async () => {
        try {
          const series = await Promise.all(
            requests.map((req) => api.computeOperator(req, snapshotCutoff === null)),
          );
          if (!cancelled) {
            const bundle: IndicatorBundle = {
              snapshot_id: snapshot.snapshot_id,
              instrument_id: snapshot.instrument_id,
              timeframe,
              series,
            };
            setIndicatorBundle(bundle);
            setIndicatorError(null);
            adapterRef.current?.setIndicators(series);
          }
        } catch (err) {
          if (!cancelled) {
            setIndicatorError(
              err instanceof Error ? err.message : t("指标获取失败", "Failed to load indicators"),
            );
          }
        } finally {
          if (!cancelled) setIndicatorsLoading(false);
        }
      })();
    }, liveTick === "" ? 0 : 450);

    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [snapshot, timeframe, liveTick, ctx.indicatorSettings, snapshotCutoff]);

  // Sync SYSTEM_LEVEL and AGENT_ANNOTATION read-only layers.
  const effectiveLevelsSource =
    replay.active && replay.replayLevels !== null
      ? replay.replayLevels
      : ctx.keyLevelsController.levels;

  const visibleLevels = useMemo(
    () =>
      visibleKeyLevels(
        effectiveLevelsSource,
        ctx.keyLevelFilter,
        ctx.keyLevelsController.ignoredIds,
      ),
    [
      effectiveLevelsSource,
      ctx.keyLevelFilter,
      ctx.keyLevelsController.ignoredIds,
    ],
  );

  useEffect(() => {
    adapterRef.current?.setSystemLevels(visibleLevels, {
      precision: pricePrecision,
      highlightId: ctx.highlightedLevelId,
    });
  }, [visibleLevels, pricePrecision, ctx.highlightedLevelId]);

  const effectiveStructuresSource = ctx.structuresController.structures;
  const visibleSystemStructures = useMemo(
    () =>
      visibleStructures(
        effectiveStructuresSource,
        ctx.structureFilter,
        ctx.structuresController.ignoredIds,
      ),
    [
      effectiveStructuresSource,
      ctx.structureFilter,
      ctx.structuresController.ignoredIds,
    ],
  );

  useEffect(() => {
    adapterRef.current?.setSystemStructures(visibleSystemStructures, {
      precision: pricePrecision,
      highlightId: ctx.highlightedStructureId,
    });
  }, [visibleSystemStructures, pricePrecision, ctx.highlightedStructureId]);

  useEffect(() => {
    if (stream.structures && (activePaneId === paneId || isPopout)) {
      ctx.onStreamStructures(stream.structures);
    }
  }, [stream.structures, activePaneId, paneId, isPopout, ctx.onStreamStructures]);

  const replayVisibleAnnotations = useMemo(
    () =>
      filterReplayAnnotations(ctx.annotationsController.annotations, {
        active: replay.active,
        cursorTime: replay.cursorTime,
        showAll: replay.showAllDrawings,
      }),
    [
      ctx.annotationsController.annotations,
      replay.active,
      replay.cursorTime,
      replay.showAllDrawings,
    ],
  );

  useEffect(() => {
    adapterRef.current?.setAgentAnnotations(replayVisibleAnnotations, {
      timeframe,
    });
  }, [replayVisibleAnnotations, timeframe]);

  // Report telemetry for the active pane.
  useEffect(() => {
    if (activePaneId !== paneId && !isPopout) return;
    updatePaneTelemetry({
      paneId,
      instrumentId,
      timeframe,
      stats,
      backend,
      snapshotId: snapshot?.snapshot_id ?? null,
      cutoffTs: snapshotCutoff,
    });
  }, [
    activePaneId,
    updatePaneTelemetry,
    paneId,
    isPopout,
    instrumentId,
    timeframe,
    stats,
    backend,
    snapshot,
    snapshotCutoff,
  ]);

  const liveGapCount = useMemo(
    () =>
      new Set(
        stream.bars
          .filter((ev) => ev.bar.status === "FEED_GAP")
          .map((ev) => ev.bar.timestamp),
      ).size,
    [stream.bars],
  );
  const totalGaps = stats.gaps + liveGapCount;
  const streamLive = stream.hello?.live ?? null;
  const badge = feedBadgeView(
    stream.feedHealth,
    stream.now,
    streamLive,
    stream.connection,
    locale,
    stream.channelRealtime,
  );

  const derivativeSummary = derivativeBundle?.summary;
  const liveDeriv = stream.derivatives;
  const markPrice = liveDeriv?.mark_price ?? derivativeSummary?.mark_price ?? null;
  const fundingRate = liveDeriv?.funding_rate ?? derivativeSummary?.funding_rate ?? null;
  const nextFundingTs = liveDeriv?.next_funding_ts ?? derivativeSummary?.next_funding_ts ?? null;
  const openInterest = liveDeriv?.open_interest ?? derivativeSummary?.open_interest ?? null;
  const hasOiHistory = derivativeSummary?.oi_history_available ?? false;

  return (
    <div
      className="chart-pane"
      data-testid={`chart-pane-${paneId}`}
      onPointerDown={() => ctx.setActivePaneId(paneId)}
    >
      <div className="chart-pane-toolbar">
        <div className="chart-pane-selectors">
          <button
            type="button"
            className="chart-symbol-trigger"
            data-testid={`instrument-select-${paneId}`}
            title={t("选择或检索标的 (⌘K)", "Select or search symbol (⌘K)")}
            aria-label={t("选择或检索标的", "Select or search symbol")}
            onClick={() =>
              ctx.onOpenSymbolSearch({
                currentInstrumentId: instrumentId,
                onPicked: (picked) => {
                  link.setSymbol(picked.instrument_id, timeframe);
                },
              })
            }
          >
            <Search size={13} className="chart-symbol-trigger-icon" />
            {venueGroups.length > 1 && activeVenue && (
              <span className="chart-symbol-trigger-venue">
                {venueShortLabel(activeVenue)}
              </span>
            )}
            <span className="chart-symbol-trigger-name">
              {spec ? symbolDisplayName(spec) : instrumentId}
            </span>
            <ChevronDown size={13} className="chart-symbol-trigger-caret" />
          </button>

          <div className="tf-pills" role="group" aria-label={t("周期", "Timeframe")}>
            {TIMEFRAMES.map((tf) => (
              <button
                key={tf}
                type="button"
                className={`tf-pill${tf === timeframe ? " is-active" : ""}`}
                data-testid={`tf-pill-${paneId}-${tf}`}
                onClick={() => link.setTimeframe(tf)}
              >
                {tf}
              </button>
            ))}
          </div>

          {!(ctx.readOnly || replay.active) && (
            <div
              className="order-tool-group"
              role="group"
              aria-label={t("图表下单工具", "Chart order gesture tools")}
              data-testid={`order-tools-${paneId}`}
            >
              <button
                type="button"
                className={`order-tool-btn order-tool-btn--long${activeOrderTool === "LONG" ? " is-active" : ""}`}
                data-testid={`order-tool-long-${paneId}`}
                aria-pressed={activeOrderTool === "LONG"}
                onClick={() => {
                  const next = activeOrderTool === "LONG" ? null : "LONG";
                  setActiveOrderTool(next);
                  adapterRef.current?.armOrderGesture(next);
                }}
              >
                {t("做多仓位", "Long Pos")}
              </button>
              <button
                type="button"
                className={`order-tool-btn order-tool-btn--short${activeOrderTool === "SHORT" ? " is-active" : ""}`}
                data-testid={`order-tool-short-${paneId}`}
                aria-pressed={activeOrderTool === "SHORT"}
                onClick={() => {
                  const next = activeOrderTool === "SHORT" ? null : "SHORT";
                  setActiveOrderTool(next);
                  adapterRef.current?.armOrderGesture(next);
                }}
              >
                {t("做空仓位", "Short Pos")}
              </button>
            </div>
          )}
        </div>

        <div className="chart-pane-meta">
          <DerivativeSummary
            markPrice={markPrice}
            fundingRate={fundingRate}
            nextFundingTs={nextFundingTs}
            openInterest={openInterest}
            pricePrecision={pricePrecision}
            hasOiHistory={hasOiHistory}
          />
          <OrderBookSummary book={stream.book} pricePrecision={pricePrecision} />
          <FeedHealthBadge
            health={stream.feedHealth}
            now={stream.now}
            live={streamLive}
            connection={stream.connection}
            channelRealtime={stream.channelRealtime}
          />
          <span
            className={`backend-pill backend-pill--${backend}`}
            data-testid={`backend-badge-${paneId}`}
            title={
              backend === "canvas2d"
                ? t("已超出 8 图表 WebGL2 预算，使用 Canvas2D", "WebGL2 budget (>8) degraded to Canvas2D")
                : t("WebGL2 硬件加速渲染", "WebGL2 accelerated rendering")
            }
          >
            {backend === "canvas2d" ? "Canvas2D" : "WebGL2"}
          </span>
          <span className="group-pill" title={t("联动组", "Link group")}>
            {group}
          </span>
          <button
            type="button"
            className="icon-button"
            title={t("刷新图表", "Refresh chart")}
            onClick={() => {
              link.clearLocate();
              void adapterRef.current?.refresh();
            }}
          >
            <RefreshCw size={14} />
          </button>
          {!isPopout && (
            <button
              type="button"
              className="icon-button"
              data-testid={`popout-pane-${paneId}`}
              title={t("弹出独立窗口", "Popout standalone window")}
              onClick={() =>
                ctx.onPopoutChart({ paneId, instrumentId, timeframe })
              }
            >
              <ExternalLink size={14} />
            </button>
          )}
        </div>
      </div>

      <IndicatorToolbar
        settings={ctx.indicatorSettings}
        onChange={ctx.onUpdateIndicator}
        defaults={ctx.indicatorDefaults}
        savingDefault={ctx.savingIndicatorDefault}
        onSaveDefault={ctx.onSaveIndicatorDefault}
        onResetDefault={ctx.onResetIndicatorDefault}
        bundle={indicatorBundle}
        loading={indicatorsLoading}
        error={indicatorError}
        keyLevelsEnabled={isStructuresEnabled(ctx.structureFilter)}
        keyLevelsCount={visibleSystemStructures.length}
        onToggleKeyLevels={() =>
          ctx.onStructureFilterChange(toggleStructuresEnabled(ctx.structureFilter))
        }
        layersCount={
          visibleSystemStructures.length +
          ctx.drawings.filter((d) => d.deleted_at == null).length +
          ctx.annotationsController.annotations.length
        }
        onOpenLayers={isPopout ? undefined : () => ctx.onOpenRightTab("layers")}
      />

      <ReplayToolbar
        active={replay.active}
        playing={replay.playing}
        isMaster={replay.isMaster}
        cursorTime={replay.cursorTime}
        remaining={replay.remaining}
        intervalMs={replay.intervalMs}
        bounds={replay.bounds}
        tickReplayEnabled={replay.tickReplayEnabled}
        showAllDrawings={replay.showAllDrawings}
        timeframe={timeframe}
        tfMs={tfMs}
        underflowWarning={replay.underflowWarning}
        onStartAt={(from) => void replay.startAt(from)}
        onStepBar={replay.stepBar}
        onStepTick={replay.stepTick}
        onPlay={replay.play}
        onPause={replay.pause}
        onStop={replay.stop}
        onChangeSpeed={replay.changeSpeed}
        onToggleTickReplay={replay.toggleTickReplay}
        onToggleShowAllDrawings={replay.toggleShowAllDrawings}
      />

      {!replay.active && (
        <FeedAlert
          health={stream.feedHealth}
          now={stream.now}
          live={streamLive}
          connection={stream.connection}
          channelRealtime={stream.channelRealtime}
        />
      )}

      {loadError && (
        <div className="panel-error" role="alert">
          {loadError}
        </div>
      )}

      <div className="chart-canvas-stage">
        {barrierDrag.notice && (
          <div
            className={`barrier-drag-notice barrier-drag-notice--${barrierDrag.notice.tone}`}
            role="status"
            data-testid="barrier-drag-notice"
          >
            <span className="barrier-drag-notice-text">{barrierDrag.notice.text}</span>
            <button
              type="button"
              className="barrier-drag-notice-close"
              aria-label={t("关闭提示", "Dismiss notice")}
              onClick={barrierDrag.dismissNotice}
            >
              ×
            </button>
          </div>
        )}
        <div
          ref={containerRef}
          className="chart-canvas-host"
          data-testid={`vela-host-${paneId}`}
          data-badge-status={badge.status}
          data-loaded-bars={stats.loaded}
          data-gap-count={totalGaps}
        />

        {orderGesture && !(ctx.readOnly || replay.active) && (() => {
          const stageW = containerRef.current?.clientWidth || 800;
          const stageH = containerRef.current?.clientHeight || 400;
          const cardW = 248;
          const cardH = 148;
          const rawLeft = orderGesture.anchorPx ? orderGesture.anchorPx.rightX + 12 : 16;
          const rawTop = orderGesture.anchorPx ? orderGesture.anchorPx.topY : 16;
          const left = Math.max(8, Math.min(rawLeft, Math.max(8, stageW - cardW - 12)));
          const top = Math.max(8, Math.min(rawTop, Math.max(8, stageH - cardH - 12)));
          const noAccount = paperAccountIds.length === 0 && !effectiveSubAccount;
          const formatPctBadge = (raw?: string | null) => {
            if (!raw) return "";
            const n = Number(raw);
            return Number.isFinite(n) ? ` (${(n * 100).toFixed(2)}%)` : "";
          };

          return (
            <div
              className="order-gesture-preview"
              data-testid="order-gesture-preview"
              data-phase={orderGesture.phase}
              data-preview-ms={orderPreviewMs ?? undefined}
              style={{ left: `${left}px`, top: `${top}px` }}
              role="region"
              aria-label={t("临时仓位定仓预演", "Order gesture sizing preview")}
            >
              <div className="order-gesture-preview-head">
                <div className="order-gesture-preview-title">
                  <span
                    className={`order-gesture-dir ${orderGesture.direction === "LONG" ? "is-long" : "is-short"}`}
                  >
                    {orderGesture.direction === "LONG"
                      ? t("做多 LONG", "LONG")
                      : t("做空 SHORT", "SHORT")}
                  </span>
                  <span>R:R {orderGesture.rr.toFixed(2)}</span>
                </div>
                <button
                  type="button"
                  className="icon-button"
                  aria-label={t("清除临时仓位", "Clear order gesture")}
                  onClick={() => {
                    adapterRef.current?.clearOrderGesture();
                    setActiveOrderTool(null);
                    setOrderGesture(null);
                  }}
                >
                  <X size={13} />
                </button>
              </div>

              <div className="order-gesture-prices">
                <span>
                  {t("入", "E")} {orderGesture.entryStr}
                </span>
                <span>
                  {t("损", "SL")} {orderGesture.stopStr}
                </span>
                <span>
                  {t("盈", "TP")} {orderGesture.targetStr}
                </span>
              </div>

              {noAccount && (
                <div className="order-gesture-warn" data-testid="order-gesture-no-account" role="status">
                  {t(
                    "未发现运行中的子账户，暂无法实时预演定仓；可点击“生成交易草案”后手动输入子账户 ID。",
                    "No sub-accounts detected for live sizing preview; click Create Draft to enter a sub-account ID.",
                  )}
                </div>
              )}

              {!orderGesture.validGeometry && (
                <div className="order-gesture-warn" data-testid="order-gesture-invalid-geometry" role="alert">
                  {t(
                    "止损或止盈与入场价重合，请拖动句柄分离后再生成草案。",
                    "Stop or target coincides with entry; drag handles apart before creating a draft.",
                  )}
                </div>
              )}

              {orderGestureNotice && (
                <div className="order-gesture-warn" role="alert">
                  {orderGestureNotice}
                </div>
              )}

              {orderPreviewError && (
                <div className="order-gesture-warn" role="alert">
                  {orderPreviewError}
                </div>
              )}

              {orderPreview?.rejected_reason && (
                <div className="paper-sizing-rejection" data-testid="order-gesture-rejected" role="alert">
                  {orderPreview.rejected_reason}
                </div>
              )}

              {orderPreview && !noAccount && orderGesture.validGeometry && (
                <dl className="order-gesture-metrics">
                  <div>
                    <dt>{t("数量", "Qty")}</dt>
                    <dd data-testid="order-gesture-qty">{orderPreview.quantity ?? "—"}</dd>
                  </div>
                  <div>
                    <dt>{t("风险 (R)", "Risk (R)")}</dt>
                    <dd data-testid="order-gesture-risk">
                      {orderPreview.risk_amount_r ?? "—"}
                      {formatPctBadge(orderPreview.risk_pct_of_equity)}
                    </dd>
                  </div>
                  <div>
                    <dt>{t("强平距离", "Liq dist")}</dt>
                    <dd data-testid="order-gesture-liq">
                      {orderPreview.est_liq_distance ?? "—"}
                      {formatPctBadge(orderPreview.est_liq_distance_pct)}
                    </dd>
                  </div>
                </dl>
              )}

              <div className="order-gesture-actions">
                <span className="order-gesture-subaccount">
                  {effectiveSubAccount
                    ? `${t("子账户", "Acct")}: ${effectiveSubAccount}`
                    : t("未选子账户", "No sub-account")}
                </span>
                {orderGesture.drawingId && (
                  <button
                    type="button"
                    className="primary-button"
                    data-testid="order-gesture-create-draft"
                    disabled={!orderGesture.validGeometry || creatingDraftBusy}
                    onClick={() =>
                      void handleCreateDraftFromGesture({
                        drawingId: orderGesture.drawingId!,
                        direction: orderGesture.direction,
                        entry: orderGesture.entryStr,
                        stop: orderGesture.stopStr,
                        target: orderGesture.targetStr,
                        validGeometry: orderGesture.validGeometry,
                      })
                    }
                  >
                    {creatingDraftBusy
                      ? t("生成中…", "Creating…")
                      : t("生成交易草案", "Create Draft")}
                  </button>
                )}
              </div>
            </div>
          );
        })()}

        {inspectedStructure && (
          <div
            className="layer-inspect-popover"
            data-testid="structure-inspect-popover"
            role="dialog"
          >
            <div className="layer-inspect-head">
              <strong>
                {inspectedStructure.kind} ·{" "}
                {formatStructureGeometry(inspectedStructure, pricePrecision)}
              </strong>
              <button
                type="button"
                className="icon-button"
                onClick={() => setInspectedStructure(null)}
              >
                <X size={13} />
              </button>
            </div>
            <span className="layer-inspect-meta" title={inspectedStructure.detector_id}>
              {detectorLabel(inspectedStructure.detector_id)}
              {inspectedStructure.source_timeframe ? ` · ${inspectedStructure.source_timeframe}` : ""}
              {" · "}
              {t("只读系统市场结构（不可拖拽）", "Read-only system structure (non-draggable)")}
            </span>
            {!(ctx.readOnly || replay.active) && (
              <div className="layer-inspect-actions">
                <button
                  type="button"
                  className="ghost-button"
                  onClick={() => {
                    void ctx.onAdoptStructure(inspectedStructure);
                    setInspectedStructure(null);
                  }}
                >
                  {t("采纳为画线", "Adopt drawing")}
                </button>
                <button
                  type="button"
                  className="ghost-button"
                  onClick={() => {
                    ctx.onSeedAlert({
                      price: structureMidPrice(inspectedStructure),
                      instrumentId,
                      timeframe,
                    });
                    ctx.onOpenRightTab("alerts");
                    setInspectedStructure(null);
                  }}
                >
                  {t("创建预警", "Create alert")}
                </button>
              </div>
            )}
          </div>
        )}

        {inspectedLevel && (
          <div
            className="layer-inspect-popover"
            data-testid="key-level-inspect-popover"
            role="dialog"
          >
            <div className="layer-inspect-head">
              <strong>
                {keyLevelKindLabel(inspectedLevel.kind)} ·{" "}
                {formatLevelPrice(inspectedLevel, pricePrecision)}
              </strong>
              <button
                type="button"
                className="icon-button"
                onClick={() => setInspectedLevel(null)}
              >
                <X size={13} />
              </button>
            </div>
            <span className="layer-inspect-meta">
              {t("只读系统关键位（不可拖拽）", "Read-only system key level (non-draggable)")}
            </span>
            {!(ctx.readOnly || replay.active) && (
              <div className="layer-inspect-actions">
                <button
                  type="button"
                  className="ghost-button"
                  onClick={() => {
                    void ctx.onAdoptKeyLevel(inspectedLevel);
                    setInspectedLevel(null);
                  }}
                >
                  {t("采纳为画线", "Adopt drawing")}
                </button>
                <button
                  type="button"
                  className="ghost-button"
                  onClick={() => {
                    ctx.onSeedAlert({
                      levelId: inspectedLevel.level_id,
                      levelKind: inspectedLevel.kind,
                      instrumentId,
                      timeframe,
                    });
                    ctx.onOpenRightTab("alerts");
                    setInspectedLevel(null);
                  }}
                >
                  {t("创建预警", "Create alert")}
                </button>
              </div>
            )}
          </div>
        )}

        {inspectedAnnotation && (
          <div
            className="layer-inspect-popover"
            data-testid="annotation-inspect-popover"
            role="dialog"
          >
            <div className="layer-inspect-head">
              <strong>
                {inspectedAnnotation.label} ·{" "}
                {annotationPriceLabel(inspectedAnnotation)}
              </strong>
              <button
                type="button"
                className="icon-button"
                onClick={() => setInspectedAnnotation(null)}
              >
                <X size={13} />
              </button>
            </div>
            <span className="layer-inspect-meta">
              {t("只读智能体标注（不可拖拽）", "Read-only agent annotation (non-draggable)")}
            </span>
            {!(ctx.readOnly || replay.active) && inspectedAnnotation.status === "ACTIVE" && (
              <div className="layer-inspect-actions">
                <button
                  type="button"
                  className="ghost-button"
                  onClick={() => {
                    void ctx.onAdoptAnnotation(inspectedAnnotation);
                    setInspectedAnnotation(null);
                  }}
                >
                  {t("采纳为画线", "Adopt drawing")}
                </button>
                <button
                  type="button"
                  className="ghost-button"
                  onClick={() => {
                    void ctx.annotationsController.dismiss(inspectedAnnotation.annotation_id);
                    setInspectedAnnotation(null);
                  }}
                >
                  {t("忽略", "Dismiss")}
                </button>
              </div>
            )}
          </div>
        )}
      </div>

      <BarrierDragConfirmModal
        proposal={barrierDrag.pendingProposal}
        submitting={barrierDrag.submitting}
        onCancel={barrierDrag.handleCancelModal}
        onConfirm={(proposal, reason) => {
          void barrierDrag.handleConfirmModal(proposal, reason);
        }}
      />

      <div className="chart-pane-footer">
        <span>
          {t(`已加载 ${stats.loaded} 根 · 断流 ${totalGaps} 处`, `${stats.loaded} bars · ${totalGaps} gaps`)}
        </span>
        <span className="vela-inline-attribution">
          <a href={VELA_ATTRIBUTION.url} target="_blank" rel="noreferrer">
            {VELA_ATTRIBUTION.label}
          </a>
        </span>
      </div>
    </div>
  );
}
