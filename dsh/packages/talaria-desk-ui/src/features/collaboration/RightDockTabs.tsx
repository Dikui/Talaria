export type RightDockTab = "collab" | "alerts";

interface RightDockTabsProps {
  active: RightDockTab;
  alertUnread: number;
  onSelect: (tab: RightDockTab) => void;
  onClose: () => void;
}

/** 右栏共用的 tab 条：会诊与预警只切换不并排，避免互相覆盖。 */
export function RightDockTabs({
  active,
  alertUnread,
  onSelect,
  onClose,
}: RightDockTabsProps) {
  return (
    <div className="right-dock-tabs" data-testid="right-dock-tabs">
      <div className="right-dock-tablist" role="tablist" aria-label="右栏面板">
        <button
          type="button"
          role="tab"
          aria-selected={active === "collab"}
          className={`right-dock-tab${active === "collab" ? " is-active" : ""}`}
          onClick={() => onSelect("collab")}
        >
          协同会诊
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={active === "alerts"}
          className={`right-dock-tab${active === "alerts" ? " is-active" : ""}`}
          onClick={() => onSelect("alerts")}
        >
          预警{alertUnread > 0 ? ` ${alertUnread}` : ""}
        </button>
      </div>
      <button
        type="button"
        className="right-dock-close"
        aria-label="收起右栏"
        onClick={onClose}
      >
        ×
      </button>
    </div>
  );
}
