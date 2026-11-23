import { InstrumentSelect } from "../features/instruments/InstrumentSelect";
import type { InstrumentSpec } from "@talaria/contracts";
import { GROUP_LABELS, type LinkGroup, type PanelLink } from "./link";
import { t } from "../lib/i18n";
import { panelLabel, TIMEFRAMES } from "./panels";
import { useLinkSnapshot } from "./usePanelLink";

const englishGroups: Record<LinkGroup, string> = { red: "Red", blue: "Blue", green: "Green", none: "Unlinked" };
function groupLabel(id: LinkGroup) { return t(GROUP_LABELS[id], englishGroups[id]); }

export function LinkControls({ link }: { link: PanelLink }) {
  const { group, peers } = useLinkSnapshot(link);
  const sameGroupPeers = group === "none" ? [] : peers.filter((peer) => peer.group === group);
  return (
    <div className="link-controls" data-link-group={group}>
      <label className="link-group">
        <span className="link-dot" aria-hidden="true" />
        <select
          aria-label={t("颜色联动组", "Color link group")}
          value={group}
          onChange={(event) => link.setGroup(event.target.value as LinkGroup)}
        >
          {(Object.keys(GROUP_LABELS) as LinkGroup[]).map((id) => (
            <option key={id} value={id}>{groupLabel(id)}</option>
          ))}
        </select>
      </label>
      <details className="link-peers">
        <summary>{t("同组", "Linked panels")} {sameGroupPeers.length}</summary>
        <ul>
          {sameGroupPeers.length === 0 ? <li>{t("暂无其他面板", "No other panels")}</li> : sameGroupPeers.map((peer) => (
            <li key={peer.from}>
              <span>{panelLabel(peer.panel)} · {groupLabel(peer.group)}</span>
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
      <div className="timeframes" role="group" aria-label={t("K 线周期", "Candle timeframe")}>
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
