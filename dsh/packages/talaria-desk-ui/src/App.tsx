import { useCallback, useEffect, useRef, useState } from "react";
import type { Chart } from "klinecharts";

import type {
  CollabWindow,
  CompactBar,
  DerivativeBundle,
  IndicatorBundle,
  IndicatorName,
  IndicatorSeries,
  InstrumentSpec,
  SnapshotMeta,
} from "@talaria/contracts";

import { AlertPanel, AlertToast, type AlertFormSeed } from "./features/alerts/AlertPanel";
import { AnnotationLayer } from "./features/annotations/AnnotationLayer";
import { AnnotationPanel } from "./features/annotations/AnnotationPanel";
import { activeAnnotations } from "./features/annotations/model";
import { useAnnotations } from "./features/annotations/useAnnotations";
import { useAlerts } from "./features/alerts/useAlerts";
import { DerivativeSummary } from "./features/derivatives/DerivativeSummary";
import {
  FeedAlert,
  FeedHealthBadge,
  OrderBookSummary,
} from "./features/feed/FeedIndicators";
import { CollaborationPanel } from "./features/collaboration/CollaborationPanel";
import { RightDockTabs, type RightDockTab } from "./features/collaboration/RightDockTabs";
import { SelectionOverlay } from "./features/collaboration/SelectionOverlay";
import { useCollaborationStatus } from "./features/collaboration/useCollaborationStatus";
import { feedBadgeView } from "./features/feed/health";
import { useMarketStream } from "./features/feed/useMarketStream";
import { InstrumentSelect } from "./features/instruments/InstrumentSelect";
import { IndicatorToolbar } from "./features/indicators/IndicatorToolbar";
import {
  defaultIndicatorSettings,
  enabledIndicatorNames,
  needsOperatorRequest,
  operatorParams,
  settingsSignature,
  type IndicatorSetting,
  type IndicatorSettings,
} from "./features/indicators/config";
import { DrawingInspector } from "./features/drawings/DrawingInspector";
import { DrawingManager } from "./features/drawings/DrawingManager";
import { DrawingOverlay } from "./features/drawings/DrawingOverlay";
import { DrawingToolbar } from "./features/drawings/DrawingToolbar";
import { useDrawings } from "./features/drawings/useDrawings";
import { useReadOnlyViewport } from "./features/drawings/useReadOnlyViewport";
import { KeyLevelLayer } from "./features/key-levels/KeyLevelLayer";
import { KeyLevelPanel } from "./features/key-levels/KeyLevelPanel";
import {
  defaultKeyLevelFilter,
  visibleKeyLevels,
  type KeyLevelFilter,
} from "./features/key-levels/model";
import { useKeyLevels } from "./features/key-levels/useKeyLevels";
import { MarketChart, type ChartStats, type OlderPage } from "./features/market-chart/MarketChart";
import { ScannerPanel } from "./features/scanner/ScannerPanel";
import { useScanner } from "./features/scanner/useScanner";
import { WatchlistPanel } from "./features/watchlist/WatchlistPanel";
import { ApiError, api } from "./lib/api";
import { toKLineSeries } from "./lib/chartData";
import { applyTheme, resolveInitialTheme, THEME_STORAGE_KEY, type Theme } from "./lib/theme";

const TIMEFRAMES = ["1m", "5m", "15m", "1h", "4h", "1d"] as const;
const DEFAULT_INSTRUMENT = "BTC-USDT-PERP";
const PAGE_SIZE = 300;

const cutoffFormatter = new Intl.DateTimeFormat("zh-CN", {
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  hour12: false,
});

interface ErrorInfo {
  code: string;
  message: string;
}

function describeError(error: unknown): ErrorInfo {
  if (error instanceof ApiError) {
    return { code: error.code, message: error.message };
  }
  if (error instanceof Error) {
    return { code: "CLIENT_ERROR", message: error.message };
  }
  return { code: "UNKNOWN_ERROR", message: "未知错误" };
}

function feedLabel(feedMode: string | null): string {
  if (feedMode === "fixture") {
    return "离线样本";
  }
  if (feedMode === "bybit") {
    return "Bybit 实时";
  }
  if (feedMode === "replay") {
    return "回放";
  }
  return "行情源未知";
}

export interface DeskWorkbenchProps {
  /** true 时渲染在 DSH 插槽内：不覆盖宿主主题，铺满插槽高度。 */
  embedded?: boolean;
}

export function DeskWorkbench({ embedded = false }: DeskWorkbenchProps) {
  const shellRef = useRef<HTMLDivElement>(null);
  const [instruments, setInstruments] = useState<InstrumentSpec[]>([]);
  const [instrumentId, setInstrumentId] = useState(DEFAULT_INSTRUMENT);
  const [timeframe, setTimeframe] = useState<string>("1m");
  const [feedMode, setFeedMode] = useState<string | null>(null);
  const [snapshot, setSnapshot] = useState<SnapshotMeta | null>(null);
  const [derivativeBundle, setDerivativeBundle] = useState<DerivativeBundle | null>(null);
  const [bars, setBars] = useState<CompactBar[]>([]);
  const [hasMoreForward, setHasMoreForward] = useState(false);
  const [stats, setStats] = useState<ChartStats>({ loaded: 0, gaps: 0 });
  const [loading, setLoading] = useState(true);
  const [initialFailed, setInitialFailed] = useState(false);
  const [error, setError] = useState<ErrorInfo | null>(null);
  const [refreshNonce, setRefreshNonce] = useState(0);
  const [theme, setTheme] = useState<Theme>(() => resolveInitialTheme());
  const [indicatorSettings, setIndicatorSettings] = useState<IndicatorSettings>(
    defaultIndicatorSettings,
  );
  const [indicatorBundle, setIndicatorBundle] = useState<IndicatorBundle | null>(null);
  const [indicatorsLoading, setIndicatorsLoading] = useState(false);
  const [indicatorError, setIndicatorError] = useState<string | null>(null);
  const [chart, setChart] = useState<Chart | null>(null);
  const [drawingManagerOpen, setDrawingManagerOpen] = useState(false);
  const [keyLevelFilter, setKeyLevelFilter] = useState<KeyLevelFilter>(
    defaultKeyLevelFilter,
  );
  const [keyLevelPanelOpen, setKeyLevelPanelOpen] = useState(true);
  const [scannerOpen, setScannerOpen] = useState(true);
  const [rightTab, setRightTab] = useState<RightDockTab | null>("alerts");
  const [alertSeed, setAlertSeed] = useState<AlertFormSeed | null>(null);
  const [selection, setSelection] = useState<CollabWindow | null>(null);
  const [selecting, setSelecting] = useState(false);
  const [snapshotCutoff, setSnapshotCutoff] = useState<number | null>(null);
  const readOnlyViewport = useReadOnlyViewport();
  const stream = useMarketStream(instrumentId, timeframe);
  const alerts = useAlerts();
  const scanner = useScanner();
  const collaboration = useCollaborationStatus();

  const chooseInstrument = useCallback((next: string) => {
    setSnapshotCutoff(null);
    setInstrumentId(next);
  }, []);

  const chooseTimeframe = useCallback((next: string) => {
    setSnapshotCutoff(null);
    setTimeframe(next);
  }, []);

  useEffect(() => {
    setSelection(null);
    setSelecting(false);
  }, [instrumentId, timeframe]);

  useEffect(() => {
    if (!selecting) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setSelecting(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [selecting]);

  useEffect(() => {
    if (embedded) {
      if (shellRef.current !== null) {
        applyTheme(theme, shellRef.current);
      }
    } else {
      applyTheme(theme);
    }
    localStorage.setItem(THEME_STORAGE_KEY, theme);
  }, [theme, embedded]);

  useEffect(() => {
    let cancelled = false;
    api
      .health()
      .then((health) => {
        if (!cancelled) {
          setFeedMode(health.feed_mode);
        }
      })
      .catch(() => {
        if (!cancelled) {
          setFeedMode(null);
        }
      });
    api
      .instruments()
      .then((items) => {
        if (!cancelled) {
          setInstruments(items);
        }
      })
      .catch(() => {
        if (!cancelled) {
          setInstruments([]);
        }
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setInitialFailed(false);
    setError(null);
    setSnapshot(null);
    setDerivativeBundle(null);
    setBars([]);
    setStats({ loaded: 0, gaps: 0 });
    setHasMoreForward(false);

    (async () => {
      try {
        const meta = await api.createSnapshot(
          instrumentId,
          [timeframe],
          snapshotCutoff,
        );
        if (cancelled) {
          return;
        }
        const page = await api.bars(meta.snapshot_id, timeframe, {
          limit: PAGE_SIZE,
        });
        if (cancelled) {
          return;
        }
        setSnapshot(meta);
        setBars(page.bars);
        setHasMoreForward(page.has_more_forward);
        const series = toKLineSeries(page.bars);
        setStats({ loaded: series.points.length, gaps: series.gapCount });
        if (page.bars.length === 0 || meta.bar_counts[timeframe] === 0) {
          setInitialFailed(true);
          setError({
            code: "EMPTY_SNAPSHOT",
            message: "快照中没有可展示的历史 K 线。",
          });
        }
      } catch (caught) {
        if (!cancelled) {
          setInitialFailed(true);
          setError(describeError(caught));
        }
      } finally {
        if (!cancelled) {
          setLoading(false);
        }
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [instrumentId, timeframe, refreshNonce, snapshotCutoff]);

  useEffect(() => {
    if (snapshot === null) {
      setDerivativeBundle(null);
      return;
    }
    let cancelled = false;
    api
      .derivatives(snapshot.snapshot_id, timeframe)
      .then((bundle) => {
        if (!cancelled) {
          setDerivativeBundle(bundle);
        }
      })
      .catch(() => {
        if (!cancelled) {
          setDerivativeBundle(null);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [snapshot, timeframe]);

  const updateIndicator = useCallback(
    (name: IndicatorName, patch: Partial<IndicatorSetting>) => {
      setIndicatorSettings((previous) => ({
        ...previous,
        [name]: { ...previous[name], ...patch },
      }));
    },
    [],
  );

  const enabledNames = enabledIndicatorNames(indicatorSettings);
  const indicatorSignature = settingsSignature(enabledNames, indicatorSettings);
  const latestLive = stream.bars[stream.bars.length - 1];
  const liveTick =
    latestLive === undefined
      ? ""
      : `${latestLive.bar.timestamp}:${latestLive.revision}:${latestLive.confirmed}`;

  useEffect(() => {
    if (snapshot === null) {
      setIndicatorBundle(null);
      setIndicatorError(null);
      setIndicatorsLoading(false);
      return;
    }
    if (enabledNames.length === 0) {
      setIndicatorBundle(null);
      setIndicatorError(null);
      setIndicatorsLoading(false);
      return;
    }
    let cancelled = false;
    const timer = window.setTimeout(
      () => {
        setIndicatorsLoading(true);
        (async () => {
          try {
            let series: IndicatorSeries[];
            if (!needsOperatorRequest(enabledNames, indicatorSettings)) {
              const bundle = await api.indicators(
                snapshot.snapshot_id,
                timeframe,
                enabledNames,
                PAGE_SIZE,
                true,
              );
              series = bundle.series;
            } else {
              // 非默认参数只走单指标 POST；历史与指标数学仍全部在 Python。
              series = await Promise.all(
                enabledNames.map((name) =>
                  api.computeOperator({
                    snapshot_id: snapshot.snapshot_id,
                    timeframe,
                    operator: name,
                    params: operatorParams(name, indicatorSettings[name]),
                    limit: PAGE_SIZE,
                  }, true),
                ),
              );
            }
            if (!cancelled) {
              setIndicatorBundle({
                snapshot_id: snapshot.snapshot_id,
                instrument_id: snapshot.instrument_id,
                timeframe,
                series,
              });
              setIndicatorError(null);
            }
          } catch (caught) {
            if (!cancelled) {
              setIndicatorError(describeError(caught).message);
            }
          } finally {
            if (!cancelled) {
              setIndicatorsLoading(false);
            }
          }
        })();
      },
      liveTick === "" ? 0 : 550,
    );
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [snapshot, timeframe, indicatorSignature, liveTick, indicatorSettings]);

  const loadOlder = useCallback(
    async (beforeTs: number): Promise<OlderPage> => {
      if (!snapshot) {
        return { bars: [], hasMoreForward: false };
      }
      const page = await api.bars(snapshot.snapshot_id, timeframe, {
        limit: PAGE_SIZE,
        beforeTs,
      });
      return { bars: page.bars, hasMoreForward: page.has_more_forward };
    },
    [snapshot, timeframe],
  );

  const pricePrecision =
    instruments.find((item) => item.instrument_id === instrumentId)
      ?.price_precision ?? 2;
  const volumePrecision =
    instruments.find((item) => item.instrument_id === instrumentId)
      ?.qty_precision ?? 3;

  const showEmpty = !loading && (initialFailed || stats.loaded === 0);
  const derivativeSummary = derivativeBundle?.summary;
  const liveDerivative = stream.derivatives;
  const markPrice = liveDerivative?.mark_price ?? derivativeSummary?.mark_price ?? null;
  const fundingRate =
    liveDerivative?.funding_rate ?? derivativeSummary?.funding_rate ?? null;
  const nextFundingTs =
    liveDerivative?.next_funding_ts ?? derivativeSummary?.next_funding_ts ?? null;
  const openInterest =
    liveDerivative?.open_interest ?? derivativeSummary?.open_interest ?? null;
  const hasOiHistory = derivativeSummary?.oi_history_available ?? false;
  const visibleIndicators =
    snapshot !== null &&
    indicatorBundle !== null &&
    indicatorBundle.snapshot_id === snapshot.snapshot_id &&
    indicatorBundle.timeframe === timeframe
      ? indicatorBundle
      : null;
  const oiSeries = visibleIndicators?.series.find(
    (item) => item.name === "OI_DIVERGENCE",
  );
  const oiPaneEmpty =
    oiSeries !== undefined &&
    oiSeries.points.every((point) => point.values.value === null);

  useEffect(() => {
    if (stream.alert !== null) {
      alerts.ingestTrigger(stream.alert);
    }
  }, [stream.alert, alerts.ingestTrigger]);

  useEffect(() => {
    if (stream.scanner !== null) {
      scanner.ingest(stream.scanner);
    }
  }, [stream.scanner, scanner.ingest]);

  const locateTrigger = useCallback(
    (trigger: { instrument_id: string; timeframe: string; triggered_ts: number }) => {
      setSnapshotCutoff(trigger.triggered_ts > 0 ? trigger.triggered_ts : null);
      setInstrumentId(trigger.instrument_id);
      setTimeframe(trigger.timeframe);
      alerts.markSeen();
    },
    [alerts.markSeen],
  );

  const refresh = () => {
    setSnapshotCutoff(null);
    setRefreshNonce((nonce) => nonce + 1);
  };
  const effectiveFeedMode = stream.hello?.feed_mode ?? feedMode;
  const streamLive = stream.hello?.live ?? null;
  const badge = feedBadgeView(
    stream.feedHealth,
    stream.now,
    streamLive,
    stream.connection,
  );
  // 实时 FEED_GAP 按 timestamp 去重，同一缺口的多次 revision 只计一处。
  const liveGapCount = new Set(
    stream.bars
      .filter((event) => event.bar.status === "FEED_GAP")
      .map((event) => event.bar.timestamp),
  ).size;
  const gapCount = stats.gaps + liveGapCount;
  const liveStatus =
    stream.hello === null
      ? "实时流连接中"
      : streamLive
        ? `${badge.label} · 已推送 ${stream.bars.length} 根`
        : "离线样本（无实时流）";
  const drawings = useDrawings({
    chart,
    instrumentId,
    timeframe,
    pricePrecision,
    readOnly: readOnlyViewport,
  });
  const annotations = useAnnotations({ instrumentId });
  const activeAnnotationCount = activeAnnotations(annotations.annotations).length;
  const keyLevels = useKeyLevels({
    snapshotId: snapshot?.snapshot_id ?? null,
    instrumentId,
    timeframe,
    streamed: stream.levels,
  });
  const visibleLevels = visibleKeyLevels(
    keyLevels.levels,
    keyLevelFilter,
    keyLevels.ignoredIds,
  );
  const keyLevelInset =
    keyLevelPanelOpen && !readOnlyViewport ? 278 : 0;

  /** 会诊专用：命中当前快照就直接复用，否则走快通道再冻结一次。 */
  const freezeForConsultation = useCallback(async () => {
    if (
      snapshot !== null &&
      snapshot.instrument_id === instrumentId &&
      snapshot.timeframes.includes(timeframe)
    ) {
      return snapshot;
    }
    try {
      return await api.createSnapshot(instrumentId, [timeframe]);
    } catch {
      return null;
    }
  }, [snapshot, instrumentId, timeframe]);

  const dockTabs =
    rightTab === null ? undefined : (
      <RightDockTabs
        active={rightTab}
        alertUnread={alerts.unread}
        annotationCount={activeAnnotationCount}
        onSelect={(tab) => {
          setRightTab(tab);
          if (tab === "alerts") alerts.markSeen();
          if (tab !== "collab") setSelecting(false);
        }}
        onClose={() => {
          setRightTab(null);
          setSelecting(false);
        }}
      />
    );

  return (
    <div
      ref={shellRef}
      className={`talaria-desk app-shell ${
        embedded ? "talaria-desk--embedded" : "talaria-desk--standalone"
      }`}
      data-theme={theme}
    >
      <header className="app-header">
        <div className="brand">
          <h1>Talaria 分析工作台</h1>
          <p className="brand-subtitle">本地历史行情 · 确定性快照</p>
        </div>
        <div className="header-controls">
          <InstrumentSelect
            instruments={instruments}
            value={instrumentId}
            disabled={loading}
            onChange={chooseInstrument}
          />
          <div className="timeframes" role="group" aria-label="K 线周期">
            {TIMEFRAMES.map((item) => (
              <button
                key={item}
                type="button"
                className={`timeframe-button${item === timeframe ? " is-active" : ""}`}
                aria-pressed={item === timeframe}
                disabled={loading}
                onClick={() => chooseTimeframe(item)}
              >
                {item}
              </button>
            ))}
          </div>
          <div className="header-actions">
            <DerivativeSummary
              markPrice={markPrice}
              fundingRate={fundingRate}
              nextFundingTs={nextFundingTs}
              openInterest={openInterest}
              pricePrecision={pricePrecision}
              hasOiHistory={hasOiHistory}
            />
            <span
              className={`feed-badge feed-badge--${effectiveFeedMode ?? "unknown"}`}
              data-testid="feed-mode"
            >
              {feedLabel(effectiveFeedMode)}
            </span>
            <span
              className={`host-badge host-badge--${collaboration.tone}`}
              data-testid="host-status"
              role="status"
              title={collaboration.status?.message ?? "正在检测协同宿主"}
            >
              {collaboration.label}
            </span>
            <FeedHealthBadge
              health={stream.feedHealth}
              now={stream.now}
              live={streamLive}
              connection={stream.connection}
            />
            <OrderBookSummary
              book={stream.book}
              pricePrecision={pricePrecision}
            />
            <button
              type="button"
              className={`ghost-button scanner-toggle${scannerOpen ? " is-active" : ""}`}
              aria-pressed={scannerOpen}
              onClick={() => {
                setScannerOpen((open) => !open);
                if (!scannerOpen) {
                  scanner.markSeen();
                }
              }}
            >
              异动 {scanner.triggers.length}
            </button>
            <button
              type="button"
              className={`ghost-button alerts-toggle${rightTab === "alerts" ? " is-active" : ""}`}
              aria-pressed={rightTab === "alerts"}
              onClick={() => {
                setRightTab(rightTab === "alerts" ? null : "alerts");
                if (rightTab !== "alerts") {
                  alerts.markSeen();
                }
              }}
            >
              预警{alerts.unread > 0 ? ` ${alerts.unread}` : ""}
            </button>
            <button
              type="button"
              className={`ghost-button annotations-toggle${rightTab === "annotations" ? " is-active" : ""}`}
              aria-pressed={rightTab === "annotations"}
              onClick={() => {
                setRightTab(rightTab === "annotations" ? null : "annotations");
              }}
            >
              标注{activeAnnotationCount > 0 ? ` ${activeAnnotationCount}` : ""}
            </button>
            <button
              type="button"
              className={`ghost-button collaboration-toggle${rightTab === "collab" ? " is-active" : ""}`}
              aria-pressed={rightTab === "collab"}
              onClick={() => {
                setRightTab(rightTab === "collab" ? null : "collab");
                setSelecting(false);
              }}
            >
              协同会诊
            </button>
            {snapshotCutoff !== null && (
              <button
                type="button"
                className="ghost-button locate-badge"
                onClick={refresh}
                title="回到最新快照"
              >
                触发时刻 · 返回最新
              </button>
            )}
            <button
              type="button"
              className="ghost-button"
              aria-label={theme === "dark" ? "切换为浅色主题" : "切换为深色主题"}
              onClick={() => setTheme(theme === "dark" ? "light" : "dark")}
            >
              {theme === "dark" ? "浅色" : "深色"}
            </button>
            <button
              type="button"
              className="primary-button"
              onClick={refresh}
              disabled={loading}
            >
              刷新快照
            </button>
          </div>
        </div>
      </header>

      <main className="workspace">
        {error !== null && (
          <div className="error-banner" role="alert" data-testid="error-banner">
            <strong>{error.code}</strong>
            <span>{error.message}</span>
          </div>
        )}

        <FeedAlert
          health={stream.feedHealth}
          now={stream.now}
          live={streamLive}
          connection={stream.connection}
        />

        <WatchlistPanel
          instruments={instruments}
          selectedInstrumentId={instrumentId}
          onSelect={chooseInstrument}
        />

        {rightTab === "alerts" && (
          <AlertPanel
            controller={alerts}
            instruments={instruments}
            drawings={drawings.drawings}
            currentInstrument={instrumentId}
            currentTimeframe={timeframe}
            formSeed={alertSeed}
            onLocate={locateTrigger}
            onClose={() => setRightTab(null)}
            tabBar={dockTabs}
          />
        )}

        {rightTab === "annotations" && (
          <AnnotationPanel
            controller={annotations}
            instrumentId={instrumentId}
            snapshotId={snapshot?.snapshot_id ?? null}
            readOnly={readOnlyViewport}
            tabBar={dockTabs}
            onClose={() => setRightTab(null)}
            onAdopted={() => {
              drawings.refresh();
            }}
            onError={(message) => setError({ code: "ANNOTATION_ERROR", message })}
          />
        )}

        {rightTab === "collab" && (
          <CollaborationPanel
            snapshot={snapshot}
            freezeSnapshot={freezeForConsultation}
            instrumentId={instrumentId}
            timeframe={timeframe}
            pricePrecision={pricePrecision}
            chart={chart}
            bars={bars}
            drawings={drawings.drawings}
            selection={selection}
            selecting={selecting}
            onToggleSelecting={() => setSelecting((value) => !value)}
            onClearSelection={() => setSelection(null)}
            hostView={collaboration}
            readOnlyViewport={readOnlyViewport}
            tabBar={dockTabs}
            onClose={() => setRightTab(null)}
          />
        )}

        <section
          className={`chart-panel${keyLevelPanelOpen ? "" : " key-levels-collapsed"}`}
          aria-label="K 线主图"
        >
          {loading ? (
            <div className="loading-state" data-testid="loading-state" aria-live="polite">
              <div className="skeleton" aria-hidden="true">
                <span />
                <span />
                <span />
                <span />
                <span />
                <span />
              </div>
              <p>正在回填历史 K 线…</p>
            </div>
          ) : showEmpty ? (
            <div className="empty-state" data-testid="empty-state">
              <div className="empty-mark" aria-hidden="true">
                K
              </div>
              <h2>尚未加载到历史行情</h2>
              <p>
                请检查网络连接；也可以使用 <code>--fixture-dir</code>{" "}
                启动离线样本模式后再试。
              </p>
              <button type="button" className="primary-button" onClick={refresh}>
                重试
              </button>
            </div>
          ) : (
            snapshot && (
              <>
                <IndicatorToolbar
                  settings={indicatorSettings}
                  onChange={updateIndicator}
                  bundle={visibleIndicators}
                  loading={indicatorsLoading}
                  error={indicatorError}
                />
                {oiPaneEmpty && (
                  <div
                    className="derivative-pane-empty"
                    data-testid="derivative-pane-empty"
                    role="status"
                  >
                    该合约暂无持仓量历史，OI 背离暂不可计算。
                  </div>
                )}
                {!readOnlyViewport && (
                  <DrawingToolbar
                    controller={drawings}
                    managerOpen={drawingManagerOpen}
                    onToggleManager={() => setDrawingManagerOpen((open) => !open)}
                  />
                )}
                <MarketChart
                  key={snapshot.snapshot_id}
                  instrumentId={instrumentId}
                  timeframe={timeframe}
                  pricePrecision={pricePrecision}
                  volumePrecision={volumePrecision}
                  theme={theme}
                  initialBars={bars}
                  initialHasMoreForward={hasMoreForward}
                  loadOlder={loadOlder}
                  liveBars={stream.bars}
                  indicatorSeries={visibleIndicators?.series ?? []}
                  onStats={setStats}
                  onLoadError={(caught) => setError(describeError(caught))}
                  overlay={
                    <>
                      <KeyLevelLayer
                        chart={chart}
                        levels={visibleLevels}
                        pricePrecision={pricePrecision}
                        rightInset={keyLevelInset}
                      />
                      <AnnotationLayer
                        chart={chart}
                        annotations={annotations.annotations}
                        timeframe={timeframe}
                        readOnly={readOnlyViewport}
                        onAdopt={(annotation) => {
                          void annotations
                            .adopt(annotation.annotation_id)
                            .then((record) => {
                              if (
                                record?.adopted_drawing_id !== null &&
                                record?.adopted_drawing_id !== undefined
                              ) {
                                drawings.refresh();
                              }
                            });
                        }}
                        onDismiss={(annotation) => {
                          void annotations.dismiss(annotation.annotation_id);
                        }}
                      />
                      <DrawingOverlay chart={chart} controller={drawings} />
                      <SelectionOverlay
                        chart={chart}
                        active={selecting && rightTab === "collab"}
                        pricePrecision={pricePrecision}
                        onSelect={(window) => {
                          setSelection(window);
                          setSelecting(false);
                        }}
                      />
                    </>
                  }
                  onChartReady={setChart}
                />
                <ScannerPanel
                  controller={scanner}
                  open={scannerOpen}
                  onToggle={() => setScannerOpen(false)}
                  onLocate={locateTrigger}
                  readOnly={readOnlyViewport}
                />
                {keyLevelPanelOpen ? (
                  <KeyLevelPanel
                    levels={visibleLevels}
                    allLevels={keyLevels.levels.filter(
                      (level) => !keyLevels.ignoredIds.has(level.level_id),
                    )}
                    filter={keyLevelFilter}
                    onFilterChange={setKeyLevelFilter}
                    precision={pricePrecision}
                    ignoredCount={keyLevels.ignoredIds.size}
                    onIgnore={keyLevels.ignore}
                    ignoringId={keyLevels.ignoringId}
                    adoptedIds={keyLevels.adoptedIds}
                    adoptingId={keyLevels.adoptingId}
                    onAdopt={(level) => {
                      void keyLevels.adopt(level).then((drawing) => {
                        if (drawing !== null) {
                          drawings.refresh();
                        }
                      });
                    }}
                    onAttachAlert={(level) => {
                      setAlertSeed({
                        nonce: Date.now(),
                        levelId: level.level_id,
                        levelKind: level.kind,
                        instrumentId,
                        timeframe,
                      });
                      setRightTab("alerts");
                    }}
                    loading={keyLevels.loading}
                    error={keyLevels.error}
                    readOnly={readOnlyViewport}
                    notes={keyLevels.notes}
                    onClose={() => setKeyLevelPanelOpen(false)}
                  />
                ) : (
                  <button
                    type="button"
                    className="key-level-open"
                    data-testid="key-level-open"
                    onClick={() => setKeyLevelPanelOpen(true)}
                  >
                    关键位 {visibleLevels.length}
                  </button>
                )}
                {!readOnlyViewport && drawings.error !== null && (
                  <div className="drawing-error" role="alert" data-testid="drawing-error">
                    <span>{drawings.error}</span>
                    <button type="button" aria-label="关闭画线错误" onClick={drawings.clearError}>
                      ×
                    </button>
                  </div>
                )}
                {!readOnlyViewport && drawings.selected !== null && (
                  <DrawingInspector
                    controller={drawings}
                    timeframe={timeframe}
                    onAttachAlert={(drawing) => {
                      setAlertSeed({
                        nonce: Date.now(),
                        drawingId: drawing.drawing_id,
                        instrumentId,
                        timeframe,
                      });
                      setRightTab("alerts");
                    }}
                  />
                )}
                {!readOnlyViewport && drawingManagerOpen && (
                  <DrawingManager
                    controller={drawings}
                    onClose={() => setDrawingManagerOpen(false)}
                  />
                )}
              </>
            )
          )}
        </section>
      </main>

      {alerts.toast !== null && (
        <AlertToast
          trigger={alerts.toast}
          onLocate={(trigger) => {
            locateTrigger(trigger);
            alerts.clearToast();
          }}
          onDismiss={alerts.clearToast}
        />
      )}

      <footer className="status-bar" data-testid="status-bar" aria-live="polite">
        {snapshot ? (
          <>
            <span>快照 {snapshot.snapshot_id.slice(0, 8)}</span>
            <span className="status-divider">·</span>
            <span>截止 {cutoffFormatter.format(new Date(snapshot.cutoff_ts))}</span>
            <span className="status-divider">·</span>
            <span data-testid="loaded-count">已加载 {stats.loaded} 根</span>
            <span className="status-divider">·</span>
            <span data-testid="gap-count">断流 {gapCount} 处</span>
            <span className="status-divider">·</span>
            <span data-testid="live-status">{liveStatus}</span>
          </>
        ) : (
          <span>{loading ? "正在准备快照…" : "尚未创建快照"}</span>
        )}
      </footer>
    </div>
  );
}

export default function App() {
  return <DeskWorkbench />;
}
