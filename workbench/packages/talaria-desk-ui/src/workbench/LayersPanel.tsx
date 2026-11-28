import { AnnotationPanel } from "../features/annotations/AnnotationPanel";
import type { AnnotationsController } from "../features/annotations/useAnnotations";
import { DrawingManager, type DrawingManagerProps } from "../features/drawings/DrawingManager";
import { StructurePanel, type StructurePanelProps } from "../features/structures/StructurePanel";
import { t, useLocale } from "../lib/i18n";
import type { LayerSourceTab } from "./layoutStore";

export interface LayersPanelProps {
  sourceFilter: LayerSourceTab;
  onSourceChange: (source: LayerSourceTab) => void;
  structureProps: StructurePanelProps;
  drawingProps: DrawingManagerProps;
  annotationProps: {
    controller: AnnotationsController;
    readOnly: boolean;
    onAdopted?: () => void;
  };
}

export function LayersPanel({
  sourceFilter,
  onSourceChange,
  structureProps,
  drawingProps,
  annotationProps,
}: LayersPanelProps) {
  useLocale();

  const structureCount = structureProps.structures.length;
  const drawingCount = drawingProps.drawings.filter((d) => d.deleted_at == null).length;
  const annotationCount = annotationProps.controller.annotations.length;
  const totalCount = structureCount + drawingCount + annotationCount;

  return (
    <div className="wb-panel-shell" data-testid="layers-panel">
      <div
        className="wb-panel-subtabs wb-panel-subtabs--wrap"
        role="tablist"
        aria-label={t("图层来源筛选", "Layer source filter")}
      >
        <button
          type="button"
          role="tab"
          aria-selected={sourceFilter === "all"}
          className={`wb-subtab-btn${sourceFilter === "all" ? " is-active" : ""}`}
          data-testid="layers-source-all"
          onClick={() => onSourceChange("all")}
        >
          <span>{t("全部", "All")}</span>
          <span className="wb-subtab-count">{totalCount}</span>
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={sourceFilter === "structure"}
          className={`wb-subtab-btn${sourceFilter === "structure" ? " is-active" : ""}`}
          data-testid="layers-source-structure"
          onClick={() => onSourceChange("structure")}
        >
          <span>{t("策略", "Strategy")}</span>
          <span className="wb-subtab-count">{structureCount}</span>
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={sourceFilter === "drawing"}
          className={`wb-subtab-btn${sourceFilter === "drawing" ? " is-active" : ""}`}
          data-testid="layers-source-drawing"
          onClick={() => onSourceChange("drawing")}
        >
          <span>{t("人工", "Manual")}</span>
          <span className="wb-subtab-count">{drawingCount}</span>
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={sourceFilter === "annotation"}
          className={`wb-subtab-btn${sourceFilter === "annotation" ? " is-active" : ""}`}
          data-testid="layers-source-annotation"
          onClick={() => onSourceChange("annotation")}
        >
          <span>{t("智能体", "Agent")}</span>
          <span className="wb-subtab-count">{annotationCount}</span>
        </button>
      </div>

      <div
        className={`wb-panel-shell-body${
          sourceFilter === "all" ? " wb-panel-shell-body--scroll" : ""
        }`}
      >
        {sourceFilter === "structure" && <StructurePanel {...structureProps} />}
        {sourceFilter === "drawing" && <DrawingManager {...drawingProps} />}
        {sourceFilter === "annotation" && <AnnotationPanel {...annotationProps} />}

        {sourceFilter === "all" && (
          <div className="wb-layers-stack" data-testid="layers-all-stack">
            <section className="wb-layers-section">
              <div className="wb-layers-section-head">
                <span>{t("来源：策略 · 市场结构", "Source: Strategy · Structures")}</span>
                <span className="wb-subtab-count">{structureCount}</span>
              </div>
              <div className="wb-nested-panel">
                <StructurePanel {...structureProps} />
              </div>
            </section>

            <section className="wb-layers-section">
              <div className="wb-layers-section-head">
                <span>{t("来源：人工 · 画线", "Source: Manual · Drawings")}</span>
                <span className="wb-subtab-count">{drawingCount}</span>
              </div>
              <div className="wb-nested-panel">
                <DrawingManager {...drawingProps} />
              </div>
            </section>

            <section className="wb-layers-section">
              <div className="wb-layers-section-head">
                <span>{t("来源：智能体 · 只读标注", "Source: Agent · Annotations")}</span>
                <span className="wb-subtab-count">{annotationCount}</span>
              </div>
              <div className="wb-nested-panel">
                <AnnotationPanel {...annotationProps} />
              </div>
            </section>
          </div>
        )}
      </div>
    </div>
  );
}
