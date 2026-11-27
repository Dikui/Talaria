import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Check, Plus, Search, X } from "lucide-react";
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
  existingInstruments: InstrumentSpec[];
  onClose: () => void;
  onInstrumentsChanged: (next: InstrumentSpec[]) => void;
  onPicked: (spec: InstrumentSpec) => void | Promise<void>;
}

const DEBOUNCE_MS = 220;

function formatError(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  if (error instanceof Error) return error.message;
  return t("检索标的失败，请检查网络连接", "Failed to search instruments");
}

export function InstrumentSearchModal({
  open,
  title,
  actionLabel,
  existingInstruments,
  onClose,
  onInstrumentsChanged,
  onPicked,
}: InstrumentSearchModalProps) {
  const locale = useLocale();
  const inputRef = useRef<HTMLInputElement | null>(null);
  const [query, setQuery] = useState("");
  const [venueFilter, setVenueFilter] = useState<string>("");
  const [results, setResults] = useState<CatalogInstrument[]>([]);
  const [loading, setLoading] = useState(false);
  const [addingId, setAddingId] = useState<string | null>(null);
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
    for (const item of results) {
      if (item.spec.venue) set.add(item.spec.venue);
    }
    return Array.from(set).sort();
  }, [existingInstruments, results]);

  const runSearch = useCallback(async (q: string, venue: string) => {
    setLoading(true);
    setError(null);
    try {
      const resp = await api.searchInstruments(q, {
        venue: venue || undefined,
        limit: 50,
      });
      setResults(resp.results);
    } catch (err) {
      setError(formatError(err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (!open) return;
    const timer = window.setTimeout(() => {
      void runSearch(query, venueFilter);
    }, query.trim() ? DEBOUNCE_MS : 0);
    return () => window.clearTimeout(timer);
  }, [open, query, venueFilter, runSearch]);

  useEffect(() => {
    if (!open) return;
    window.setTimeout(() => inputRef.current?.focus(), 20);
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        onClose();
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [open, onClose]);

  if (!open) return null;

  const handleSelectItem = async (item: CatalogInstrument) => {
    if (addingId) return;
    setError(null);
    const isRegistered = item.registered || registeredIds.has(item.spec.instrument_id);
    if (isRegistered) {
      await onPicked(item.spec);
      onClose();
      return;
    }
    setAddingId(item.spec.instrument_id);
    try {
      const res = await api.addInstrument(item.spec);
      onInstrumentsChanged(res.instruments);
      setResults((prev) =>
        prev.map((row) =>
          row.spec.instrument_id === res.instrument.instrument_id
            ? { ...row, registered: true }
            : row,
        ),
      );
      await onPicked(res.instrument);
      onClose();
    } catch (err) {
      setError(formatError(err));
    } finally {
      setAddingId(null);
    }
  };

  return (
    <div
      className="symbol-search-backdrop"
      data-testid="symbol-search-modal"
      role="dialog"
      aria-modal="true"
      aria-label={title ?? t("检索并添加标的", "Search & Add Symbol")}
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div className="symbol-search-dialog">
        <header className="symbol-search-header">
          <div className="symbol-search-title-row">
            <Search size={15} />
            <h2>{title ?? t("检索并添加标的", "Search & Add Symbol")}</h2>
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
                "输入标的代码或名称（如 DOGE、SUI、TSLA、BTC）…",
                "Search symbol or name (e.g. DOGE, SUI, TSLA, BTC)…",
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
            <div className="symbol-search-venues" role="group" aria-label={t("筛选数据源", "Filter venue")}>
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
          {loading && results.length === 0 ? (
            <p className="panel-empty">{t("正在从交易所检索目录…", "Searching exchange catalog…")}</p>
          ) : results.length === 0 ? (
            <p className="panel-empty">
              {t("未找到匹配的标的，请尝试其他关键词。", "No matching symbols found.")}
            </p>
          ) : (
            <ul className="symbol-search-list">
              {results.map((item) => {
                const isRegistered =
                  item.registered || registeredIds.has(item.spec.instrument_id);
                const busy = addingId === item.spec.instrument_id;
                return (
                  <li key={`${item.spec.venue}:${item.spec.instrument_id}`} className="symbol-search-item">
                    <button
                      type="button"
                      className="symbol-search-item-btn"
                      data-testid={`symbol-search-item-${item.spec.instrument_id}`}
                      disabled={Boolean(addingId)}
                      onClick={() => void handleSelectItem(item)}
                    >
                      <div className="symbol-search-item-main">
                        <span className="symbol-search-item-id">
                          {symbolDisplayName(item.spec)}
                        </span>
                        <span className="watchlist-venue-pill">
                          {venueDisplayName(item.spec.venue, locale)}
                        </span>
                        <span className="symbol-search-item-raw">{item.spec.symbol}</span>
                      </div>
                      <div className="symbol-search-item-meta">
                        <span className="symbol-search-item-spec">
                          {item.display_name ?? item.spec.asset_class}
                          {" · "}
                          tick {item.spec.tick_size} / step {item.spec.qty_step}
                        </span>
                      </div>
                      <span
                        className={`symbol-search-item-action${
                          isRegistered ? " is-registered" : ""
                        }`}
                      >
                        {busy ? (
                          t("添加中…", "Adding…")
                        ) : isRegistered ? (
                          <>
                            <Check size={12} /> {actionLabel ?? t("打开", "Open")}
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
              })}
            </ul>
          )}
        </div>
      </div>
    </div>
  );
}
