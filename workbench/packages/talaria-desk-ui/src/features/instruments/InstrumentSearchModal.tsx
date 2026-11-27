import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Check, Globe, Plus, Search, X } from "lucide-react";
import type { InstrumentSpec } from "@talaria/contracts";

import {
  ApiError,
  api,
  type CatalogInstrument,
} from "../../lib/api";
import { t, useLocale } from "../../lib/i18n";
import { symbolDisplayName, venueDisplayName } from "../../workbench/venues";

export interface InstrumentSearchModalProps {
  open: boolean;
  title?: string;
  actionLabel?: string;
  currentInstrumentId?: string;
  existingInstruments: InstrumentSpec[];
  onClose: () => void;
  onInstrumentsChanged: (next: InstrumentSpec[]) => void;
  onPicked: (spec: InstrumentSpec) => void | Promise<void>;
}

const DEBOUNCE_MS = 200;

function formatError(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  if (error instanceof Error) return error.message;
  return t("检索标的失败，请检查网络连接", "Failed to search instruments");
}

function matchesQuery(spec: InstrumentSpec, query: string): boolean {
  const q = query.trim().toUpperCase();
  if (!q) return true;
  return (
    spec.instrument_id.toUpperCase().includes(q) ||
    spec.symbol.toUpperCase().includes(q) ||
    spec.venue.toUpperCase().includes(q) ||
    symbolDisplayName(spec).toUpperCase().includes(q)
  );
}

interface UnifiedRow {
  key: string;
  spec: InstrumentSpec;
  displayName?: string | null;
  registered: boolean;
}

export function InstrumentSearchModal({
  open,
  title,
  actionLabel,
  currentInstrumentId,
  existingInstruments,
  onClose,
  onInstrumentsChanged,
  onPicked,
}: InstrumentSearchModalProps) {
  const locale = useLocale();
  const inputRef = useRef<HTMLInputElement | null>(null);
  const [query, setQuery] = useState("");
  const [venueFilter, setVenueFilter] = useState<string>("");
  const [browseCatalog, setBrowseCatalog] = useState(false);
  const [remoteResults, setRemoteResults] = useState<CatalogInstrument[]>([]);
  const [loadingRemote, setLoadingRemote] = useState(false);
  const [addingId, setAddingId] = useState<string | null>(null);
  const [activeIndex, setActiveIndex] = useState(0);
  const [error, setError] = useState<string | null>(null);

  const registeredIds = useMemo(
    () => new Set(existingInstruments.map((i) => i.instrument_id)),
    [existingInstruments],
  );

  const availableVenues = useMemo(() => {
    const set = new Set<string>();
    for (const inst of existingInstruments) {
      if (inst.venue) set.add(inst.venue);
    }
    for (const item of remoteResults) {
      if (item.spec.venue) set.add(item.spec.venue);
    }
    return Array.from(set).sort();
  }, [existingInstruments, remoteResults]);

  const localRows = useMemo<UnifiedRow[]>(() => {
    return existingInstruments
      .filter((spec) => {
        if (venueFilter && spec.venue !== venueFilter) return false;
        return matchesQuery(spec, query);
      })
      .map((spec) => ({
        key: `local:${spec.venue}:${spec.instrument_id}`,
        spec,
        displayName: null,
        registered: true,
      }));
  }, [existingInstruments, venueFilter, query]);

  const remoteRows = useMemo<UnifiedRow[]>(() => {
    return remoteResults
      .filter((item) => {
        if (item.registered || registeredIds.has(item.spec.instrument_id)) return false;
        if (venueFilter && item.spec.venue !== venueFilter) return false;
        return true;
      })
      .map((item) => ({
        key: `remote:${item.spec.venue}:${item.spec.instrument_id}`,
        spec: item.spec,
        displayName: item.display_name,
        registered: false,
      }));
  }, [remoteResults, registeredIds, venueFilter]);

  const allRows = useMemo(() => [...localRows, ...remoteRows], [localRows, remoteRows]);

  const shouldQueryRemote = query.trim().length > 0 || browseCatalog;

  const runSearch = useCallback(async (q: string, venue: string) => {
    setLoadingRemote(true);
    setError(null);
    try {
      const resp = await api.searchInstruments(q, {
        venue: venue || undefined,
        limit: 50,
      });
      setRemoteResults(resp.results);
    } catch (err) {
      setError(formatError(err));
    } finally {
      setLoadingRemote(false);
    }
  }, []);

  useEffect(() => {
    if (!open) {
      setQuery("");
      setVenueFilter("");
      setBrowseCatalog(false);
      setRemoteResults([]);
      setError(null);
      setActiveIndex(0);
      return;
    }
    window.setTimeout(() => inputRef.current?.focus(), 20);
  }, [open]);

  useEffect(() => {
    if (!open || !shouldQueryRemote) {
      if (!shouldQueryRemote) {
        setRemoteResults([]);
        setLoadingRemote(false);
      }
      return;
    }
    const timer = window.setTimeout(() => {
      void runSearch(query, venueFilter);
    }, query.trim() ? DEBOUNCE_MS : 0);
    return () => window.clearTimeout(timer);
  }, [open, shouldQueryRemote, query, venueFilter, runSearch]);

  useEffect(() => {
    setActiveIndex(0);
  }, [query, venueFilter, localRows.length, remoteRows.length]);

  const handleSelectRow = useCallback(
    async (row: UnifiedRow) => {
      if (addingId) return;
      setError(null);
      if (row.registered || registeredIds.has(row.spec.instrument_id)) {
        await onPicked(row.spec);
        onClose();
        return;
      }
      setAddingId(row.spec.instrument_id);
      try {
        const res = await api.addInstrument(row.spec);
        onInstrumentsChanged(res.instruments);
        setRemoteResults((prev) =>
          prev.map((item) =>
            item.spec.instrument_id === res.instrument.instrument_id
              ? { ...item, registered: true }
              : item,
          ),
        );
        await onPicked(res.instrument);
        onClose();
      } catch (err) {
        setError(formatError(err));
      } finally {
        setAddingId(null);
      }
    },
    [addingId, registeredIds, onPicked, onClose, onInstrumentsChanged],
  );

  useEffect(() => {
    if (!open) return;
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        onClose();
      } else if (e.key === "ArrowDown") {
        e.preventDefault();
        setActiveIndex((prev) => (allRows.length > 0 ? (prev + 1) % allRows.length : 0));
      } else if (e.key === "ArrowUp") {
        e.preventDefault();
        setActiveIndex((prev) =>
          allRows.length > 0 ? (prev - 1 + allRows.length) % allRows.length : 0,
        );
      } else if (e.key === "Enter" && allRows[activeIndex]) {
        e.preventDefault();
        void handleSelectRow(allRows[activeIndex]);
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [open, onClose, allRows, activeIndex, handleSelectRow]);

  if (!open) return null;

  const renderRow = (row: UnifiedRow, flatIndex: number) => {
    const isCurrent = currentInstrumentId === row.spec.instrument_id;
    const isActive = flatIndex === activeIndex;
    const busy = addingId === row.spec.instrument_id;
    return (
      <li key={row.key} className="symbol-search-item">
        <button
          type="button"
          className={`symbol-search-item-btn${isActive ? " is-active" : ""}${
            isCurrent ? " is-current" : ""
          }`}
          data-testid={`symbol-search-item-${row.spec.instrument_id}`}
          disabled={Boolean(addingId)}
          onMouseEnter={() => setActiveIndex(flatIndex)}
          onClick={() => void handleSelectRow(row)}
        >
          <div className="symbol-search-item-main">
            <span className="symbol-search-item-id">{symbolDisplayName(row.spec)}</span>
            <span className="watchlist-venue-pill">
              {venueDisplayName(row.spec.venue, locale)}
            </span>
            <span className="symbol-search-item-raw">{row.spec.symbol}</span>
            {isCurrent && (
              <span className="symbol-search-current-badge">{t("当前", "Active")}</span>
            )}
          </div>
          <div className="symbol-search-item-meta">
            <span className="symbol-search-item-spec">
              {row.displayName ?? row.spec.asset_class}
              {" · "}
              tick {row.spec.tick_size} / step {row.spec.qty_step}
            </span>
          </div>
          <span
            className={`symbol-search-item-action${row.registered ? " is-registered" : ""}`}
          >
            {busy ? (
              t("添加中…", "Adding…")
            ) : row.registered ? (
              <>
                <Check size={12} /> {actionLabel ?? t("切换", "Select")}
              </>
            ) : (
              <>
                <Plus size={12} /> {actionLabel ?? t("添加并打开", "Add & Open")}
              </>
            )}
          </span>
        </button>
      </li>
    );
  };

  return (
    <div
      className="symbol-search-backdrop"
      data-testid="symbol-search-modal"
      role="dialog"
      aria-modal="true"
      aria-label={title ?? t("选择或检索标的", "Select or Search Symbol")}
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div className="symbol-search-dialog">
        <header className="symbol-search-header">
          <div className="symbol-search-title-row">
            <Search size={15} />
            <h2>{title ?? t("选择或检索标的", "Select or Search Symbol")}</h2>
            <kbd className="symbol-search-kbd">⌘K</kbd>
          </div>
          <button
            type="button"
            className="ghost-button icon-only"
            aria-label={t("关闭", "Close")}
            data-testid="symbol-search-close"
            onClick={onClose}
          >
            <X size={15} />
          </button>
        </header>

        <div className="symbol-search-controls">
          <div className="symbol-search-input-wrap">
            <Search size={14} className="symbol-search-input-icon" />
            <input
              ref={inputRef}
              type="text"
              className="symbol-search-input"
              data-testid="symbol-search-input"
              value={query}
              placeholder={t(
                "输入代码切换已有标的，或在线检索新标的（如 DOGE、SUI、TSLA）…",
                "Filter added symbols or search exchange catalog (e.g. DOGE, SUI, TSLA)…",
              )}
              onChange={(e) => setQuery(e.target.value)}
            />
            {query && (
              <button
                type="button"
                className="symbol-search-clear"
                aria-label={t("清空", "Clear")}
                onClick={() => setQuery("")}
              >
                ×
              </button>
            )}
          </div>

          {availableVenues.length > 1 && (
            <div
              className="symbol-search-venues"
              role="group"
              aria-label={t("筛选数据源", "Filter venue")}
            >
              <button
                type="button"
                className={`symbol-search-venue-pill${venueFilter === "" ? " is-active" : ""}`}
                onClick={() => setVenueFilter("")}
              >
                {t("全部", "All")}
              </button>
              {availableVenues.map((v) => (
                <button
                  key={v}
                  type="button"
                  className={`symbol-search-venue-pill${venueFilter === v ? " is-active" : ""}`}
                  onClick={() => setVenueFilter(v)}
                >
                  {venueDisplayName(v, locale)}
                </button>
              ))}
            </div>
          )}
        </div>

        {error && (
          <p className="panel-error symbol-search-error" role="alert">
            {error}
          </p>
        )}

        <div className="symbol-search-results" data-testid="symbol-search-results">
          {localRows.length > 0 && (
            <section className="symbol-search-section">
              <div className="symbol-search-section-label">
                {t("已添加标的", "Added Symbols")} ({localRows.length})
              </div>
              <ul className="symbol-search-list">
                {localRows.map((row, idx) => renderRow(row, idx))}
              </ul>
            </section>
          )}

          {shouldQueryRemote ? (
            <section className="symbol-search-section">
              <div className="symbol-search-section-label">
                <Globe size={12} /> {t("交易所在线目录", "Exchange Catalog")}
                {loadingRemote ? ` · ${t("检索中…", "Searching…")}` : ` (${remoteRows.length})`}
              </div>
              {loadingRemote && remoteRows.length === 0 ? (
                <p className="panel-empty">
                  {t("正在从交易所检索目录…", "Searching exchange catalog…")}
                </p>
              ) : remoteRows.length === 0 ? (
                <p className="panel-empty">
                  {localRows.length > 0
                    ? t("没有更多未添加的匹配标的。", "No additional unregistered symbols found.")
                    : t("未找到匹配的标的，请尝试其他关键词。", "No matching symbols found.")}
                </p>
              ) : (
                <ul className="symbol-search-list">
                  {remoteRows.map((row, idx) => renderRow(row, localRows.length + idx))}
                </ul>
              )}
            </section>
          ) : (
            <div className="symbol-search-browse-hint">
              <span>
                {t(
                  "输入关键词即可实时检索交易所新标的",
                  "Type to search new symbols from connected exchanges",
                )}
              </span>
              <button
                type="button"
                className="ghost-button"
                data-testid="symbol-search-browse-catalog"
                onClick={() => setBrowseCatalog(true)}
              >
                <Globe size={12} /> {t("浏览交易所目录", "Browse Catalog")}
              </button>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
