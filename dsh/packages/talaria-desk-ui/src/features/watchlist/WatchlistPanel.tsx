import { useCallback, useEffect, useMemo, useState } from "react";
import type { FormEvent } from "react";

import type {
  InstrumentSpec,
  Watchlist,
  WatchlistGroup,
  WatchlistItem,
} from "@talaria/contracts";

import { ApiError, api } from "../../lib/api";
import { t, useLocale } from "../../lib/i18n";
import {
  isEmptyWatchlist,
  isInstrumentSelected,
  moveId,
  quoteView,
} from "./model";

const POLL_MS = 8_000;

interface WatchlistPanelProps {
  instruments: InstrumentSpec[];
  selectedInstrumentId: string;
  onSelect: (instrumentId: string) => void;
}

function errorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    return error.message;
  }
  if (error instanceof Error) {
    return error.message;
  }
  return t("自选观察池请求失败", "Watchlist request failed");
}

function precisionByInstrument(
  instruments: InstrumentSpec[],
): Map<string, number> {
  return new Map(
    instruments.map((instrument) => [
      instrument.instrument_id,
      instrument.price_precision,
    ]),
  );
}

function ItemRow({
  item,
  group,
  precision,
  selected,
  busy,
  onSelect,
  onMove,
  onRemove,
}: {
  item: WatchlistItem;
  group: WatchlistGroup;
  precision: number;
  selected: boolean;
  busy: boolean;
  onSelect: (instrumentId: string) => void;
  onMove: (direction: -1 | 1) => void;
  onRemove: () => void;
}) {
  const locale = useLocale();
  const quote = quoteView(item, precision, locale);
  const index = group.items.findIndex(
    (entry) => entry.instrument_id === item.instrument_id,
  );
  return (
    <li className="watchlist-item-row">
      <button
        type="button"
        className={`watchlist-item${selected ? " is-selected" : ""}`}
        title={quote.title}
        aria-pressed={selected}
        onClick={() => onSelect(item.instrument_id)}
      >
        <span className="watchlist-symbol" title={item.instrument_id}>
          {item.instrument_id}
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
          <span className="watchlist-source">{quote.sourceLabel}</span>
        </span>
      </button>
      {!group.is_system && (
        <div className="watchlist-edit watchlist-item-actions">
          <button
            type="button"
            className="watchlist-icon-button"
            title={t("上移标的", "Move instrument up")}
            aria-label={t(`上移 ${item.instrument_id}`, `Move ${item.instrument_id} up`)}
            disabled={busy || index <= 0}
            onClick={() => onMove(-1)}
          >
            ↑
          </button>
          <button
            type="button"
            className="watchlist-icon-button"
            title={t("下移标的", "Move instrument down")}
            aria-label={t(`下移 ${item.instrument_id}`, `Move ${item.instrument_id} down`)}
            disabled={busy || index >= group.items.length - 1}
            onClick={() => onMove(1)}
          >
            ↓
          </button>
          <button
            type="button"
            className="watchlist-icon-button watchlist-icon-button--danger"
            title={t("从自选中移除", "Remove from watchlist")}
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
  useLocale();
  const [data, setData] = useState<Watchlist | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [newGroupName, setNewGroupName] = useState("");
  const [renamingId, setRenamingId] = useState<string | null>(null);
  const [renameDraft, setRenameDraft] = useState("");
  const [addingTo, setAddingTo] = useState<string | null>(null);
  const [addDraft, setAddDraft] = useState("");

  const precisions = useMemo(
    () => precisionByInstrument(instruments),
    [instruments],
  );

  const load = useCallback(async (quiet = false) => {
    if (!quiet) {
      setLoading(true);
    }
    try {
      const next = await api.watchlist();
      setData(next);
      setError(null);
    } catch (caught) {
      if (!quiet) {
        setError(errorMessage(caught));
      }
    } finally {
      if (!quiet) {
        setLoading(false);
      }
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    const timer = window.setInterval(() => {
      void load(true);
    }, POLL_MS);
    return () => window.clearInterval(timer);
  }, [load]);

  // 切换主图会改变图表优先级，立即重新分配配额并刷新标记。
  useEffect(() => {
    void load(true);
  }, [selectedInstrumentId, load]);

  const mutate = useCallback(async (action: () => Promise<Watchlist>) => {
    setBusy(true);
    setError(null);
    try {
      setData(await action());
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      setBusy(false);
    }
  }, []);

  const submitGroup = (event: FormEvent) => {
    event.preventDefault();
    const name = newGroupName.trim();
    if (!name || busy) {
      return;
    }
    setNewGroupName("");
    void mutate(() => api.createWatchlistGroup(name));
  };

  const submitRename = (event: FormEvent, groupId: string) => {
    event.preventDefault();
    const name = renameDraft.trim();
    if (!name || busy) {
      return;
    }
    setRenamingId(null);
    void mutate(() => api.renameWatchlistGroup(groupId, name));
  };

  const submitAdd = (event: FormEvent, group: WatchlistGroup) => {
    event.preventDefault();
    const available = instruments.filter(
      (instrument) =>
        !group.items.some(
          (item) => item.instrument_id === instrument.instrument_id,
        ),
    );
    const instrumentId =
      addDraft && available.some((item) => item.instrument_id === addDraft)
        ? addDraft
        : (available[0]?.instrument_id ?? "");
    if (!instrumentId || busy) {
      return;
    }
    setAddingTo(null);
    setAddDraft("");
    void mutate(() => api.addWatchlistItem(group.group_id, instrumentId));
  };

  const empty = isEmptyWatchlist(data?.groups ?? null);
  const canAdd = instruments.length > 0;

  return (
    <aside
      className="workspace-sidebar watchlist-panel"
      data-testid="watchlist-panel"
      aria-label={t("自选观察池", "Watchlist")}
    >
      <header className="watchlist-header">
        <div>
          <h2 className="watchlist-title">{t("自选观察池", "Watchlist")}</h2>
          <p className="watchlist-subtitle">{t("点击标的切换主图", "Click an instrument to switch the chart")}</p>
        </div>
        {data !== null && (
          <span
            className="watchlist-quota"
            title={t(`实时订阅 ${data.realtime_used}/${data.realtime_quota}，当前图表永远优先`, `Live subscriptions ${data.realtime_used}/${data.realtime_quota}; the current chart always has priority`)}
          >
            {t("实时", "Live")} {data.realtime_used}/{data.realtime_quota}
          </span>
        )}
      </header>

      {error !== null && (
        <p className="watchlist-error" role="alert">
          {error}
        </p>
      )}

      {loading && data === null ? (
        <p className="watchlist-loading">{t("正在读取自选观察池…", "Loading watchlist…")}</p>
      ) : (
        <>
          {empty && (
            <div className="watchlist-empty" data-testid="watchlist-empty">
              <h2>{t("自选观察池还是空的", "Your watchlist is empty")}</h2>
              <p>
                {t("新建一个分组，把想持续跟踪的标的放进来；点击列表即可切换主图，实时配额会优先给当前图表。", "Create a group and add the instruments you want to follow. Clicking the list switches the chart, and live quota prioritizes the current chart.")}
              </p>
            </div>
          )}

          {data !== null && data.groups.length > 0 && (
            <ul className="watchlist-groups">
              {data.groups.map((group, groupIndex) => {
                const isSystem = group.is_system || group.kind !== "MANUAL";
                const available = instruments.filter(
                  (instrument) =>
                    !group.items.some(
                      (item) =>
                        item.instrument_id === instrument.instrument_id,
                    ),
                );
                const addValue =
                  addDraft &&
                  available.some((item) => item.instrument_id === addDraft)
                    ? addDraft
                    : (available[0]?.instrument_id ?? "");
                return (
                  <li
                    className={`watchlist-group${
                      isSystem ? " watchlist-group--system" : ""
                    }`}
                    key={group.group_id}
                    data-watchlist-group={group.group_id}
                  >
                    <div className="watchlist-group-head">
                      {renamingId === group.group_id ? (
                        <form
                          className="watchlist-rename"
                          onSubmit={(event) =>
                            submitRename(event, group.group_id)
                          }
                        >
                          <input
                            className="watchlist-input"
                            value={renameDraft}
                            autoFocus
                            maxLength={60}
                            aria-label={t("分组名称", "Group name")}
                            onChange={(event) =>
                              setRenameDraft(event.target.value)
                            }
                            onKeyDown={(event) => {
                              if (event.key === "Escape") {
                                setRenamingId(null);
                              }
                            }}
                          />
                          <button
                            type="submit"
                            className="watchlist-icon-button"
                            disabled={busy}
                            title={t("保存分组名称", "Save group name")}
                          >
                            ✓
                          </button>
                          <button
                            type="button"
                            className="watchlist-icon-button"
                            disabled={busy}
                            title={t("取消重命名", "Cancel rename")}
                            onClick={() => setRenamingId(null)}
                          >
                            ×
                          </button>
                        </form>
                      ) : (
                        <>
                          <span
                            className="watchlist-group-name"
                            title={group.name}
                          >
                            {group.name}
                          </span>
                          {isSystem && (
                            <span
                              className="watchlist-system-tag"
                              title={t("扫描器动态分组，只读展示", "Scanner-generated group; read-only")}
                            >
                              {t("扫描器", "Scanner")}
                            </span>
                          )}
                          <span className="watchlist-group-count">
                            {group.items.length}
                          </span>
                          {!isSystem && (
                            <div className="watchlist-edit watchlist-group-actions">
                              <button
                                type="button"
                                className="watchlist-icon-button"
                                title={t("分组上移", "Move group up")}
                                aria-label={t(`分组 ${group.name} 上移`, `Move group ${group.name} up`)}
                                disabled={busy || groupIndex <= 0}
                                onClick={() => {
                                  const ids = data.groups.map(
                                    (entry) => entry.group_id,
                                  );
                                  void mutate(() =>
                                    api.orderWatchlistGroups(
                                      moveId(ids, group.group_id, -1),
                                    ),
                                  );
                                }}
                              >
                                ↑
                              </button>
                              <button
                                type="button"
                                className="watchlist-icon-button"
                                title={t("分组下移", "Move group down")}
                                aria-label={t(`分组 ${group.name} 下移`, `Move group ${group.name} down`)}
                                disabled={
                                  busy ||
                                  groupIndex >= data.groups.length - 1 ||
                                  data.groups[groupIndex + 1]?.is_system ===
                                    true
                                }
                                onClick={() => {
                                  const ids = data.groups.map(
                                    (entry) => entry.group_id,
                                  );
                                  void mutate(() =>
                                    api.orderWatchlistGroups(
                                      moveId(ids, group.group_id, 1),
                                    ),
                                  );
                                }}
                              >
                                ↓
                              </button>
                              <button
                                type="button"
                                className="watchlist-icon-button"
                                title={t("重命名分组", "Rename group")}
                                aria-label={t(`重命名分组 ${group.name}`, `Rename group ${group.name}`)}
                                disabled={busy}
                                onClick={() => {
                                  setRenamingId(group.group_id);
                                  setRenameDraft(group.name);
                                }}
                              >
                                ✎
                              </button>
                              <button
                                type="button"
                                className="watchlist-icon-button"
                                title={t("添加标的", "Add instrument")}
                                aria-label={t(`向分组 ${group.name} 添加标的`, `Add an instrument to ${group.name}`)}
                                disabled={busy || !canAdd}
                                onClick={() => {
                                  setAddingTo(
                                    addingTo === group.group_id
                                      ? null
                                      : group.group_id,
                                  );
                                  setAddDraft("");
                                }}
                              >
                                ＋
                              </button>
                              <button
                                type="button"
                                className="watchlist-icon-button watchlist-icon-button--danger"
                                title={t("删除分组", "Delete group")}
                                aria-label={t(`删除分组 ${group.name}`, `Delete group ${group.name}`)}
                                disabled={busy}
                                onClick={() =>
                                  void mutate(() =>
                                    api.deleteWatchlistGroup(group.group_id),
                                  )
                                }
                              >
                                ×
                              </button>
                            </div>
                          )}
                        </>
                      )}
                    </div>

                    {group.items.length === 0 ? (
                      <p className="watchlist-group-empty">
                        {t("这个分组还没有标的。", "This group has no instruments yet.")}
                      </p>
                    ) : (
                      <ul className="watchlist-items">
                        {group.items.map((item) => (
                          <ItemRow
                            key={item.instrument_id}
                            item={item}
                            group={group}
                            precision={
                              precisions.get(item.instrument_id) ?? 2
                            }
                            selected={isInstrumentSelected(
                              item.instrument_id,
                              selectedInstrumentId,
                            )}
                            busy={busy}
                            onSelect={onSelect}
                            onMove={(direction) => {
                              const ids = group.items.map(
                                (entry) => entry.instrument_id,
                              );
                              void mutate(() =>
                                api.orderWatchlistItems(
                                  group.group_id,
                                  moveId(ids, item.instrument_id, direction),
                                ),
                              );
                            }}
                            onRemove={() =>
                              void mutate(() =>
                                api.removeWatchlistItem(item.instrument_id),
                              )
                            }
                          />
                        ))}
                      </ul>
                    )}

                    {addingTo === group.group_id && !isSystem && (
                      <form
                        className="watchlist-form watchlist-edit"
                        onSubmit={(event) => submitAdd(event, group)}
                      >
                        <select
                          className="watchlist-select"
                          value={addValue}
                          aria-label={t("选择要添加的标的", "Select an instrument to add")}
                          disabled={busy || available.length === 0}
                          onChange={(event) => setAddDraft(event.target.value)}
                        >
                          {available.map((instrument) => (
                            <option
                              key={instrument.instrument_id}
                              value={instrument.instrument_id}
                            >
                              {instrument.instrument_id}
                            </option>
                          ))}
                        </select>
                        <button
                          type="submit"
                          className="ghost-button"
                          disabled={busy || available.length === 0}
                        >
                          {t("添加", "Add")}
                        </button>
                      </form>
                    )}
                  </li>
                );
              })}
            </ul>
          )}

          <form
            className="watchlist-form watchlist-edit"
            onSubmit={submitGroup}
          >
            <input
              className="watchlist-input"
              value={newGroupName}
              maxLength={60}
              placeholder={t("新建分组名称", "New group name")}
              aria-label={t("新建分组名称", "New group name")}
              onChange={(event) => setNewGroupName(event.target.value)}
            />
            <button
              type="submit"
              className="ghost-button"
              disabled={busy || newGroupName.trim().length === 0}
            >
              {t("新建分组", "New group")}
            </button>
          </form>
          <p className="watchlist-hint watchlist-edit">
            {t("当前图表占用实时配额，其余按分组顺序分配；超出配额显示“配额降级”。", "The current chart holds a live subscription; the rest are allocated in group order. Over-quota items show “Quota downgraded”.")}
          </p>
        </>
      )}
    </aside>
  );
}
