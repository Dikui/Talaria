import { InstrumentSelect } from "../features/instruments/InstrumentSelect";
import type { InstrumentSpec } from "@talaria/contracts";
import { GROUP_LABELS, type LinkGroup, type PanelLink } from "./link";
import { PANEL_LABELS, TIMEFRAMES } from "./panels";
import { useLinkSnapshot } from "./usePanelLink";

export function LinkControls({ link }: { link: PanelLink }) {
  const { group, peers } = useLinkSnapshot(link);
  return (
    <div className="link-controls" data-link-group={group}>
      <label className="link-group">
        <span className="link-dot" aria-hidden="true" />
        <select
          aria-label="颜色联动组"
          value={group}
          onChange={(event) => link.setGroup(event.target.value as LinkGroup)}
        >
          {Object.entries(GROUP_LABELS).map(([id, label]) => (
            <option key={id} value={id}>{label}</option>
          ))}
        </select>
      </label>
      <details className="link-peers">
        <summary>同组 {group === "none" ? 0 : peers.filter((peer) => peer.group === group).length}</summary>
        <ul>
          {peers.length === 0 ? <li>暂无其他面板</li> : peers.map((peer) => (
            <li key={peer.from}>
              <span>{PANEL_LABELS[peer.panel]} · {GROUP_LABELS[peer.group]}</span>
              <span title={peer.instrumentId}>{peer.instrumentId} · {peer.timeframe}</span>
            </li>
          ))}
        </ul>
      </details>
    </div>
  );
}

export function SymbolControls({
  link, instruments, disabled,
}: { link: PanelLink; instruments: InstrumentSpec[]; disabled?: boolean }) {
  const { instrumentId, timeframe } = useLinkSnapshot(link);
  return (
    <>
      <InstrumentSelect
        instruments={instruments}
        value={instrumentId}
        disabled={disabled}
        onChange={(next) => link.setSymbol(next, timeframe)}
      />
      <div className="timeframes" role="group" aria-label="K 线周期">
        {TIMEFRAMES.map((item) => (
          <button
            key={item}
            type="button"
            className={`timeframe-button${item === timeframe ? " is-active" : ""}`}
            aria-pressed={item === timeframe}
            disabled={disabled}
            onClick={() => link.setSymbol(instrumentId, item)}
          >
            {item}
          </button>
        ))}
      </div>
    </>
  );
}
