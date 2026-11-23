import { t, useLocale } from "../../lib/i18n";

export type RightDockTab = "collab" | "alerts" | "annotations" | "briefings";

interface RightDockTabsProps {
  active: RightDockTab;
  alertUnread: number;
  annotationCount: number;
  briefingUnread?: number;
  onSelect: (tab: RightDockTab) => void;
  onClose: () => void;
}

/** 右栏共用的 tab 条：简报、会诊与预警只切换不并排，避免互相覆盖。 */
export function RightDockTabs({
  active,
  alertUnread,
  annotationCount,
  briefingUnread = 0,
  onSelect,
  onClose,
}: RightDockTabsProps) {
  useLocale();
  return (
    <div className="right-dock-tabs" data-testid="right-dock-tabs">
      <div className="right-dock-tablist" role="tablist" aria-label={t("右栏面板", "Right dock panel")}>
        <button
          type="button"
          role="tab"
          aria-selected={active === "briefings"}
          className={`right-dock-tab${active === "briefings" ? " is-active" : ""}`}
          onClick={() => onSelect("briefings")}
        >
          {t(`简报${briefingUnread > 0 ? ` ${briefingUnread}` : ""}`, `Briefings${briefingUnread > 0 ? ` ${briefingUnread}` : ""}`)}
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={active === "collab"}
          className={`right-dock-tab${active === "collab" ? " is-active" : ""}`}
          onClick={() => onSelect("collab")}
        >
          {t("协同会诊", "Consultation")}
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={active === "annotations"}
          className={`right-dock-tab${active === "annotations" ? " is-active" : ""}`}
          onClick={() => onSelect("annotations")}
        >
          {t(`标注${annotationCount > 0 ? ` ${annotationCount}` : ""}`, `Annotations${annotationCount > 0 ? ` ${annotationCount}` : ""}`)}
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={active === "alerts"}
          className={`right-dock-tab${active === "alerts" ? " is-active" : ""}`}
          onClick={() => onSelect("alerts")}
        >
          {t(`预警${alertUnread > 0 ? ` ${alertUnread}` : ""}`, `Alerts${alertUnread > 0 ? ` ${alertUnread}` : ""}`)}
        </button>
      </div>
      <button
        type="button"
        className="right-dock-close"
        aria-label={t("收起右栏", "Collapse right dock")}
        onClick={onClose}
      >
        ×
      </button>
    </div>
  );
}
