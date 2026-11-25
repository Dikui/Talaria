import { useState } from "react";
import { Pause, Play, Rewind, SkipForward, Square, StepForward } from "lucide-react";

import { t, useLocale } from "../../lib/i18n";

export const REPLAY_SPEED_OPTIONS = [
  { label: "0.5x", intervalMs: 2000 },
  { label: "1x", intervalMs: 1000 },
  { label: "2x", intervalMs: 500 },
  { label: "5x", intervalMs: 200 },
  { label: "10x", intervalMs: 100 },
] as const;

export interface ReplayToolbarProps {
  active: boolean;
  playing: boolean;
  isMaster: boolean;
  cursorTime: number | null;
  remaining: number;
  intervalMs: number;
  bounds: { first: number; last: number } | null;
  tickReplayEnabled: boolean;
  showAllDrawings: boolean;
  timeframe: string;
  tfMs: number;
  underflowWarning?: boolean;
  onStartAt: (fromBarOpenMs: number) => void;
  onStepBar: () => void;
  onStepTick: () => void;
  onPlay: (intervalMs?: number) => void;
  onPause: () => void;
  onStop: () => void;
  onChangeSpeed: (intervalMs: number) => void;
  onToggleTickReplay: (enabled: boolean) => void;
  onToggleShowAllDrawings: (showAll: boolean) => void;
}

function toDatetimeLocalValue(tsMs: number): string {
  const date = new Date(tsMs);
  if (Number.isNaN(date.getTime())) return "";
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${date.getUTCFullYear()}-${pad(date.getUTCMonth() + 1)}-${pad(date.getUTCDate())}T${pad(date.getUTCHours())}:${pad(date.getUTCMinutes())}`;
}

function parseDatetimeLocalUtc(value: string): number | null {
  if (!value) return null;
  const parsed = Date.parse(value.endsWith("Z") ? value : `${value}:00Z`);
  return Number.isFinite(parsed) ? parsed : null;
}

function formatUtcTimestamp(tsMs: number | null): string {
  if (tsMs === null || !Number.isFinite(tsMs)) return "—";
  return new Date(tsMs).toISOString().replace("T", " ").slice(0, 16) + " UTC";
}

export function ReplayToolbar({
  active,
  playing,
  isMaster,
  cursorTime,
  remaining,
  intervalMs,
  bounds,
  tickReplayEnabled,
  showAllDrawings,
  tfMs,
  underflowWarning = false,
  onStartAt,
  onStepBar,
  onStepTick,
  onPlay,
  onPause,
  onStop,
  onChangeSpeed,
  onToggleTickReplay,
  onToggleShowAllDrawings,
}: ReplayToolbarProps) {
  useLocale();
  const [pickerOpen, setPickerOpen] = useState(false);
  const [selectedTimeInput, setSelectedTimeInput] = useState("");

  const defaultStartTs = (() => {
    if (!bounds) return null;
    const target = bounds.last - 50 * tfMs;
    return Math.max(bounds.first, Math.min(bounds.last - tfMs, target));
  })();

  if (!active) {
    return (
      <div className="replay-toolbar replay-toolbar--idle" data-testid="replay-toolbar">
        <button
          type="button"
          className="ghost-button"
          data-testid="replay-open-picker"
          disabled={!bounds}
          onClick={() => {
            if (defaultStartTs !== null) {
              setSelectedTimeInput(toDatetimeLocalValue(defaultStartTs));
            }
            setPickerOpen((prev) => !prev);
          }}
        >
          <Rewind size={13} /> {t("K线回放", "Replay")}
        </button>

        {pickerOpen && bounds && (
          <div className="replay-start-controls" data-testid="replay-start-controls">
            <label className="replay-time-field">
              <span>{t("起点(UTC)", "Start (UTC)")}</span>
              <input
                type="datetime-local"
                data-testid="replay-start-input"
                value={selectedTimeInput}
                onChange={(e) => setSelectedTimeInput(e.target.value)}
              />
            </label>
            <button
              type="button"
              className="ghost-button"
              data-testid="replay-preset-50"
              onClick={() => {
                const from = Math.max(bounds.first, bounds.last - 50 * tfMs);
                onStartAt(from);
                setPickerOpen(false);
              }}
            >
              -50 {t("根", "bars")}
            </button>
            <button
              type="button"
              className="primary-button"
              data-testid="replay-start-btn"
              onClick={() => {
                const parsed = parseDatetimeLocalUtc(selectedTimeInput);
                const from = parsed ?? defaultStartTs ?? bounds.first;
                onStartAt(from);
                setPickerOpen(false);
              }}
            >
              {t("开始回放", "Start Replay")}
            </button>
          </div>
        )}
      </div>
    );
  }

  return (
    <div className="replay-toolbar replay-toolbar--active" data-testid="replay-toolbar">
      <span className="replay-badge" data-testid="replay-active-badge">
        {t("回放中", "Replay")} · {formatUtcTimestamp(cursorTime)}
        {!isMaster ? ` (${t("联动", "Synced")})` : ""}
      </span>

      <span className="replay-alignment-tag" data-testid="replay-closed-alignment-label">
        {t("大周期按已收盘对齐", "Higher TF aligned on closed bars")}
      </span>

      <span className="replay-remaining" data-testid="replay-remaining">
        {t(`余 ${remaining} 根`, `${remaining} left`)}
      </span>

      {underflowWarning && (
        <span className="replay-warning" data-testid="replay-underflow-warning">
          {t("当前时刻早于已加载首根已收盘柱", "Cursor precedes first closed bar")}
        </span>
      )}

      <div className="replay-controls">
        {isMaster && (
          <>
            <button
              type="button"
              className="ghost-button"
              data-testid="replay-play-pause"
              onClick={() => (playing ? onPause() : onPlay(intervalMs))}
              title={playing ? t("暂停", "Pause") : t("播放", "Play")}
            >
              {playing ? <Pause size={13} /> : <Play size={13} />}
              {playing ? t("暂停", "Pause") : t("播放", "Play")}
            </button>

            <button
              type="button"
              className="ghost-button"
              data-testid="replay-step-bar"
              disabled={playing}
              onClick={onStepBar}
              title={t("逐根推进", "Step 1 bar")}
            >
              <SkipForward size={13} /> {t("逐根", "Bar")}
            </button>

            <button
              type="button"
              className="ghost-button"
              data-testid="replay-step-tick"
              disabled={playing || !tickReplayEnabled}
              onClick={onStepTick}
              title={t("逐笔推进", "Step 1 tick")}
            >
              <StepForward size={13} /> {t("逐笔", "Tick")}
            </button>

            <select
              aria-label={t("回放倍速", "Replay speed")}
              data-testid="replay-speed-select"
              value={String(intervalMs)}
              onChange={(e) => onChangeSpeed(Number(e.target.value))}
            >
              {REPLAY_SPEED_OPTIONS.map((opt) => (
                <option key={opt.intervalMs} value={opt.intervalMs}>
                  {opt.label}
                </option>
              ))}
            </select>

            <label className="replay-checkbox">
              <input
                type="checkbox"
                data-testid="replay-ticks-toggle"
                checked={tickReplayEnabled}
                onChange={(e) => onToggleTickReplay(e.target.checked)}
              />
              <span>{t("逐笔", "Ticks")}</span>
            </label>
          </>
        )}

        <label className="replay-checkbox">
          <input
            type="checkbox"
            data-testid="replay-show-all-drawings"
            checked={showAllDrawings}
            onChange={(e) => onToggleShowAllDrawings(e.target.checked)}
          />
          <span>{t("显示全部画线", "Show all drawings")}</span>
        </label>

        {isMaster && (
          <button
            type="button"
            className="ghost-button"
            data-testid="replay-stop"
            onClick={onStop}
          >
            <Square size={12} /> {t("退出", "Exit")}
          </button>
        )}
      </div>
    </div>
  );
}
