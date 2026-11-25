import { useCallback, useEffect, useMemo, useState, type FormEvent } from "react";
import type {
  InstrumentSpec,
  Watchlist,
  WatchlistGroup,
  WatchlistItem,
} from "@talaria/contracts";

import { ApiError, api } from "../../lib/api";
import { t, useLocale } from "../../lib/i18n";
import { PerspectiveTableSlot } from "../../workbench/PerspectiveTableSlot";
import {
  groupInstrumentsByVenue,
  symbolDisplayName,
  venueDisplayName,
  venueShortLabel,
} from "../../workbench/venues";
import { isEmptyWatchlist, isInstrumentSelected, moveId, quoteView } from "./model";

const POLL_MS = 8_000;

export interface WatchlistPanelProps {
  instruments: InstrumentSpec[];
  selectedInstrumentId: string;
  onSelect: (instrumentId: string) => void;
}

function errorMessage(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  if (error instanceof Error) return error.message;
  return t("自选观察池请求失败", "Watchlist request failed");
}

function ItemRow({
  item,
  group,
  spec,
  precision,
  selected,
  busy,
  onSelect,
  onMove,
  onRemove,
}: {
  item: WatchlistItem;
  group: WatchlistGroup;
  spec?: InstrumentSpec;
  precision: number;
  selected: boolean;
  busy: boolean;
  onSelect: (instrumentId: string) => void;
  onMove: (direction: -1 | 1) => void;
  onRemove: () => void;
}) {
  const locale = useLocale();
  const quote = quoteView(item, precision, locale);
  const index = group.items.findIndex((e) => e.instrument_id === item.instrument_id);
  const venueLabel = spec ? venueShortLabel(spec.venue) : null;
  const displaySymbol = spec ? symbolDisplayName(spec) : item.instrument_id;
  return (
    <li className="watchlist-item-row">
      <button
        type="button"
        className={`watchlist-item${selected ? " is-selected" : ""}`}
        title={quote.title}
        aria-pressed={selected}
        onClick={() => onSelect(item.instrument_id)}
      >
        <span
          className="watchlist-symbol"
          title={venueLabel ? `${venueLabel} · ${item.instrument_id}` : item.instrument_id}
        >
          {venueLabel && <span className="watchlist-venue-pill">{venueLabel}</span>}
          {displaySymbol}
        </span>
        <span className="watchlist-quote">
          <span className="watchlist-price">{quote.price}</span>
          <span className={`watchlist-change watchlist-change--${quote.changeTone}`}>
            {quote.change}
          </span>
          <span
            className={`watchlist-badge ${
              item.realtime
                ? "watchlist-badge--live"
                : item.degradation_reason === "QUOTA"
                  ? "watchlist-badge--degraded"
                  : "watchlist-badge--muted"
            }`}
          >
            {quote.statusLabel}
          </span>
          {quote.staleLabel !== null && (
            <span className="watchlist-badge watchlist-badge--stale">
              {quote.staleLabel}
            </span>
          )}
        </span>
      </button>
      {!group.is_system && (
        <div className="watchlist-edit watchlist-item-actions">
          <button
            type="button"
            className="watchlist-icon-button"
            aria-label={t(`上移 ${item.instrument_id}`, `Move ${item.instrument_id} up`)}
            disabled={busy || index <= 0}
            onClick={() => onMove(-1)}
          >
            ↑
          </button>
          <button
            type="button"
            className="watchlist-icon-button"
            aria-label={t(`下移 ${item.instrument_id}`, `Move ${item.instrument_id} down`)}
            disabled={busy || index >= group.items.length - 1}
            onClick={() => onMove(1)}
          >
            ↓
          </button>
          <button
            type="button"
            className="watchlist-icon-button watchlist-icon-button--danger"
            aria-label={t(`移除 ${item.instrument_id}`, `Remove ${item.instrument_id}`)}
            disabled={busy}
            onClick={onRemove}
          >
            ×
          </button>
        </div>
      )}
    </li>
  );
}

export function WatchlistPanel({
  instruments,
  selectedInstrumentId,
  onSelect,
}: WatchlistPanelProps) {
  const locale = useLocale();
  const [data, setData] = useState<Watchlist | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [newGroupName, setNewGroupName] = useState("");
  const [addingTo, setAddingTo] = useState<string | null>(null);
  const [addDraft, setAddDraft] = useState("");

  const specById = useMemo(
    () => new Map(instruments.map((inst) => [inst.instrument_id, inst])),
    [instruments],
  );
  const venueGroups = useMemo(
    () => groupInstrumentsByVenue(instruments),
    [instruments],
  );

  const load = useCallback(async (quiet = false) => {
    if (!quiet) setLoading(true);
    try {
      const next = await api.watchlist();
      setData(next);
      setError(null);
    } catch (caught) {
      if (!quiet) setError(errorMessage(caught));
    } finally {
      if (!quiet) setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    const timer = window.setInterval(() => void load(true), POLL_MS);
    return () => window.clearInterval(timer);
  }, [load]);

  useEffect(() => {
    void load(true);
  }, [selectedInstrumentId, load]);

  const mutate = async (action: () => Promise<Watchlist>) => {
    setBusy(true);
    try {
      const next = await action();
      setData(next);
      setError(null);
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      setBusy(false);
    }
  };

  const handleCreateGroup = (event: FormEvent) => {
    event.preventDefault();
    const name = newGroupName.trim();
    if (!name) return;
    void mutate(async () => {
      const next = await api.createWatchlistGroup(name);
      setNewGroupName("");
      return next;
    });
  };

  const groups = data?.groups ?? [];
  const allItems = groups.flatMap((g) => g.items);

  return (
    <section
      className="dock-panel watchlist-panel"
      data-testid="watchlist-panel"
      aria-label={t("自选观察池", "Watchlist")}
    >
      <header className="dock-panel-head">
        <div>
          <h2>{t("自选观察池", "Watchlist")}</h2>
          {data && (
            <span className="dock-panel-meta">
              {t(
                `实时配额 ${data.realtime_used}/${data.realtime_quota}`,
                `Live quota ${data.realtime_used}/${data.realtime_quota}`,
              )}
            </span>
          )}
        </div>
        <button type="button" className="ghost-button" onClick={() => void load()}>
          {t("刷新", "Refresh")}
        </button>
      </header>

      {error && (
        <p className="panel-error" role="alert">
          {error}
        </p>
      )}

      <form className="watchlist-create-form" onSubmit={handleCreateGroup}>
        <input
          type="text"
          value={newGroupName}
          placeholder={t("新建自选分组…", "New watchlist group…")}
          onChange={(e) => setNewGroupName(e.target.value)}
        />
        <button type="submit" className="ghost-button" disabled={busy || !newGroupName.trim()}>
          {t("添加", "Add")}
        </button>
      </form>

      <PerspectiveTableSlot
        rows={allItems}
        tableName="watchlist"
        fallback={
          loading && data === null ? (
            <p className="panel-empty">{t("加载自选中…", "Loading watchlist…")}</p>
          ) : isEmptyWatchlist(groups) ? (
            <p className="panel-empty">{t("暂无自选分组，请先创建一个分组。", "No watchlist groups yet.")}</p>
          ) : (
            <div className="watchlist-groups">
              {groups.map((group) => (
                <div key={group.group_id} className="watchlist-group">
                  <div className="watchlist-group-head">
                    <strong>{group.name}</strong>
                    {!group.is_system && (
                      <button
                        type="button"
                        className="ghost-button"
                        onClick={() => {
                          setAddingTo(addingTo === group.group_id ? null : group.group_id);
                          setAddDraft(instruments[0]?.instrument_id ?? "");
                        }}
                      >
                        + {t("加标的", "Symbol")}
                      </button>
                    )}
                  </div>
                  {addingTo === group.group_id && (
                    <div className="watchlist-add-row">
                      <select value={addDraft} onChange={(e) => setAddDraft(e.target.value)}>
                        {venueGroups.length > 1
                          ? venueGroups.map((venueGroup) => (
                              <optgroup
                                key={venueGroup.venue}
                                label={venueDisplayName(venueGroup.venue, locale)}
                              >
                                {venueGroup.instruments.map((inst) => (
                                  <option key={inst.instrument_id} value={inst.instrument_id}>
                                    {symbolDisplayName(inst)}
                                  </option>
                                ))}
                              </optgroup>
                            ))
                          : instruments.map((inst) => (
                              <option key={inst.instrument_id} value={inst.instrument_id}>
                                {inst.instrument_id}
                              </option>
                            ))}
                      </select>
                      <button
                        type="button"
                        className="primary-button"
                        disabled={busy || !addDraft}
                        onClick={() => {
                          void mutate(async () => {
                            const next = await api.addWatchlistItem(group.group_id, addDraft);
                            setAddingTo(null);
                            return next;
                          });
                        }}
                      >
                        {t("加入", "Add")}
                      </button>
                    </div>
                  )}
                  <ul className="watchlist-list">
                    {group.items.map((item) => {
                      const itemSpec = specById.get(item.instrument_id);
                      return (
                        <ItemRow
                          key={item.instrument_id}
                          item={item}
                          group={group}
                          spec={itemSpec}
                          precision={itemSpec?.price_precision ?? 2}
                          selected={isInstrumentSelected(item.instrument_id, selectedInstrumentId)}
                        busy={busy}
                        onSelect={onSelect}
                        onMove={(dir) => {
                          const nextIds = moveId(
                            group.items.map((i) => i.instrument_id),
                            item.instrument_id,
                            dir,
                          );
                          void mutate(() => api.orderWatchlistItems(group.group_id, nextIds));
                        }}
                          onRemove={() =>
                            void mutate(() => api.removeWatchlistItem(item.instrument_id))
                          }
                        />
                      );
                    })}
                  </ul>
                </div>
              ))}
            </div>
          )
        }
      />
    </section>
  );
}
