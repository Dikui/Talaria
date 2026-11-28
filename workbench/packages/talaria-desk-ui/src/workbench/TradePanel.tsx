import { ChevronDown, Columns2 } from "lucide-react";

import { AccountPanel } from "../features/paper/AccountPanel";
import { DraftPanel, type DraftPanelProps } from "../features/paper/DraftPanel";
import { t, useLocale } from "../lib/i18n";
import type { TradeDrawerTab } from "./layoutStore";

export interface TradePanelProps {
  activeTab: TradeDrawerTab;
  onTabChange: (tab: TradeDrawerTab) => void;
  draftPanelProps: DraftPanelProps;
  onCollapse?: () => void;
  showSplitToggle?: boolean;
}

export function TradePanel({
  activeTab,
  onTabChange,
  draftPanelProps,
  onCollapse,
  showSplitToggle = true,
}: TradePanelProps) {
  useLocale();
  const effectiveTab: TradeDrawerTab =
    !showSplitToggle && activeTab === "split" ? "account" : activeTab;

  return (
    <div className="wb-panel-shell wb-trade-shell" data-testid="trade-panel">
      <div className="wb-drawer-bar">
        <div className="wb-panel-subtabs wb-panel-subtabs--inline" role="tablist" aria-label={t("交易面板视图", "Trade panel views")}>
          <button
            type="button"
            role="tab"
            aria-selected={effectiveTab === "account"}
            className={`wb-subtab-btn${effectiveTab === "account" ? " is-active" : ""}`}
            data-testid="trade-tab-account"
            onClick={() => onTabChange("account")}
          >
            {t("模拟盘账户", "Paper Account")}
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={effectiveTab === "drafts"}
            className={`wb-subtab-btn${effectiveTab === "drafts" ? " is-active" : ""}`}
            data-testid="trade-tab-drafts"
            onClick={() => onTabChange("drafts")}
          >
            {t("暂存草案", "Staged Drafts")}
          </button>
          {showSplitToggle && (
            <button
              type="button"
              role="tab"
              aria-selected={effectiveTab === "split"}
              className={`wb-subtab-btn${effectiveTab === "split" ? " is-active" : ""}`}
              data-testid="trade-tab-split"
              onClick={() => onTabChange("split")}
              title={t("账户与草案并排展示", "Show account and drafts side by side")}
            >
              <Columns2 size={12} />
              <span>{t("并排", "Split")}</span>
            </button>
          )}
        </div>

        {onCollapse && (
          <button
            type="button"
            className="icon-button"
            data-testid="trade-drawer-collapse"
            title={t("收起交易面板", "Collapse trade drawer")}
            aria-label={t("收起交易面板", "Collapse trade drawer")}
            onClick={onCollapse}
          >
            <ChevronDown size={14} />
          </button>
        )}
      </div>

      <div className="wb-panel-shell-body">
        {effectiveTab === "account" && <AccountPanel />}
        {effectiveTab === "drafts" && <DraftPanel {...draftPanelProps} />}
        {effectiveTab === "split" && (
          <div className="wb-trade-split" data-testid="trade-split-view">
            <div className="wb-trade-split-col">
              <AccountPanel />
            </div>
            <div className="wb-trade-split-col">
              <DraftPanel {...draftPanelProps} />
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
