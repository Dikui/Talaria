import { useEffect, useMemo, useRef, useState } from "react";
import { ExternalLink, RefreshCw, X } from "lucide-react";
import type {
  AgentAnnotationRecord,
  DerivativeBundle,
  IndicatorBundle,
  KeyLevel,
  PaperRound,
  SnapshotMeta,
} from "@talaria/contracts";

import {
  VELA_ATTRIBUTION,
  roundsToBarriers,
  type ChartStats,
  type RenderBackendMode,
} from "../chart/adapter";
import { VelaChartAdapter } from "../chart/VelaChartAdapter";
import { DerivativeSummary } from "../features/derivatives/DerivativeSummary";
import { FeedAlert, FeedHealthBadge, OrderBookSummary } from "../features/feed/FeedIndicators";
import { feedBadgeView } from "../features/feed/health";
import { useMarketStream } from "../features/feed/useMarketStream";
import { IndicatorToolbar } from "../features/indicators/IndicatorToolbar";
import { indicatorRequests } from "../features/indicators/config";
import { formatLevelPrice, keyLevelKindLabel, visibleKeyLevels } from "../features/key-levels/model";
import { annotationPriceLabel } from "../features/annotations/model";
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
  pickInstrumentForVenue,
  symbolDisplayName,
  venueDisplayName,
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
  const [inspectedLevel, setInspectedLevel] = useState<KeyLevel | null>(null);
  const [inspectedAnnotation, setInspectedAnnotation] =
    useState<AgentAnnotationRecord | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

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
  const activeVenueInstruments = useMemo(() => {
    if (venueGroups.length <= 1) return ctx.instruments;
    return (
      venueGroups.find((item) => item.venue === activeVenue)?.instruments ?? ctx.instruments
    );
  }, [venueGroups, activeVenue, ctx.instruments]);
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
        setInspectedLevel(level);
      },
      onSelectAnnotation: (annotation) => {
        setInspectedLevel(null);
        setInspectedAnnotation(annotation);
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
    setInspectedAnnotation(null);
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
    return () => {
      cancelled = true;
    };
  }, [instrumentId]);

  useEffect(() => {
    if (ctx.selectedRunRound && ctx.selectedRunRound.instrument_id === instrumentId) {
      adapterRef.current?.setExecutionBarriers([runRoundToBarrier(ctx.selectedRunRound)]);
      return;
    }
    adapterRef.current?.setExecutionBarriers(
      snapshotCutoff === null ? roundsToBarriers(barriers) : [],
    );
  }, [barriers, snapshotCutoff, ctx.selectedRunRound, instrumentId]);

  const runTradeMarkers = useMemo(
    () => roundsToTradeExecutions(ctx.runsController.rounds, tfMs, instrumentId),
    [ctx.runsController.rounds, tfMs, instrumentId],
  );

  useEffect(() => {
    adapterRef.current?.setTradeMarkers(runTradeMarkers);
  }, [runTradeMarkers]);

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
          {venueGroups.length > 1 && (
            <select
              className="chart-venue-select"
              data-testid={`venue-select-${paneId}`}
              aria-label={t("选择数据源", "Select data source")}
              value={activeVenue}
              onChange={(e) => {
                const next = pickInstrumentForVenue(
                  ctx.instruments,
                  e.target.value,
                  instrumentId,
                );
                if (next) {
                  link.setSymbol(next.instrument_id, timeframe);
                }
              }}
            >
              {venueGroups.map((venueGroup) => (
                <option key={venueGroup.venue} value={venueGroup.venue}>
                  {venueDisplayName(venueGroup.venue, locale)}
                </option>
              ))}
            </select>
          )}
          <select
            data-testid={`instrument-select-${paneId}`}
            aria-label={t("选择标的", "Select instrument")}
            value={instrumentId}
            onChange={(e) => link.setSymbol(e.target.value, timeframe)}
          >
            {(activeVenueInstruments.length > 0
              ? activeVenueInstruments
              : [{ instrument_id: instrumentId, venue: activeVenue }]
            ).map((item) => (
              <option key={item.instrument_id} value={item.instrument_id}>
                {symbolDisplayName(item)}
              </option>
            ))}
          </select>

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
        <div
          ref={containerRef}
          className="chart-canvas-host"
          data-testid={`vela-host-${paneId}`}
          data-badge-status={badge.status}
          data-loaded-bars={stats.loaded}
          data-gap-count={totalGaps}
        />

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
