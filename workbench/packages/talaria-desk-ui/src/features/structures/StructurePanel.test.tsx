import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { StructureFeature } from "@talaria/contracts";

import { setLocale } from "../../lib/i18n";
import { StructurePanel } from "./StructurePanel";
import { defaultStructureFilter, type StructureFilter } from "./model";

function makeFeature(id: string, detectorId: string): StructureFeature {
  return {
    structure_id: id,
    instrument_id: "BTC-USDT-PERP",
    detector_id: detectorId,
    family: "TRENDLINE",
    category: "TREND",
    kind: "TRENDLINE_SUPPORT",
    evidence_class: "OBSERVED",
    timeframe: "1m",
    source_timeframe: "1m",
    geometry: {
      kind: "SLOPE_LINE",
      anchor_start: { ts: 1_700_000_000_000, price: "68000" },
      anchor_end: { ts: 1_700_000_600_000, price: "68500" },
      scale: "LINEAR",
      extend_right: true,
    },
    strength: 0.85,
    status: "ACTIVE",
    ts_available: 1_700_000_600_000,
    formed_ts: 1_700_000_000_000,
    analysis_window: { from_ts: 1_700_000_000_000, to_ts: 1_700_000_600_000 },
  };
}

describe("StructurePanel detector plug/unplug", () => {
  beforeEach(() => {
    setLocale("zh");
  });

  it("expands detector plug/unplug in simple mode, shows versions & 0-count items, and toggles one or all", () => {
    const f1 = makeFeature("st_1", "pivot_trendline@1");
    const f2 = makeFeature("st_2", "pivot_trendline@2");
    const onFilterChange = vi.fn<(next: StructureFilter) => void>();
    const filter: StructureFilter = { ...defaultStructureFilter, enabled: true, viewMode: "simple" };

    render(
      <StructurePanel
        structures={[f1, f2]}
        allStructures={[f1, f2]}
        registeredDetectors={["gap_levels@1", "pivot_trendline@1", "pivot_trendline@2"]}
        activeTimeframe="1m"
        filter={filter}
        onFilterChange={onFilterChange}
        precision={2}
        ignoredCount={0}
        onIgnore={vi.fn()}
        ignoringId={null}
        onAdopt={vi.fn()}
        adoptingId={null}
        adoptedIds={new Set()}
        onAttachAlert={vi.fn()}
        loading={false}
        error={null}
        readOnly={false}
      />,
    );

    const toggle = screen.getByTestId("structure-detectors-toggle");
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    expect(screen.queryByTestId("structure-detector-pivot_trendline@1")).toBeNull();

    fireEvent.click(toggle);
    expect(toggle.getAttribute("aria-expanded")).toBe("true");

    const gapBtn = screen.getByTestId("structure-detector-gap_levels@1");
    expect(gapBtn.textContent).toContain("跳空缺口");
    expect(gapBtn.textContent).toContain("v1");
    expect(gapBtn.textContent).toContain("0");

    const v1Btn = screen.getByTestId("structure-detector-pivot_trendline@1");
    const v2Btn = screen.getByTestId("structure-detector-pivot_trendline@2");
    expect(v1Btn.textContent).toContain("摆动趋势线");
    expect(v1Btn.textContent).toContain("v1");
    expect(v2Btn.textContent).toContain("v2");

    fireEvent.click(v1Btn);
    expect(onFilterChange).toHaveBeenLastCalledWith({
      ...filter,
      disabledDetectorIds: ["pivot_trendline@1"],
    });

    fireEvent.click(screen.getByTestId("structure-detectors-disable-all"));
    expect(onFilterChange).toHaveBeenLastCalledWith({
      ...filter,
      disabledDetectorIds: [
        "confluence@1",
        "gap_levels@1",
        "pivot_trendline@1",
        "pivot_trendline@2",
      ],
    });

    const enableAllBtn = screen.getByTestId(
      "structure-detectors-enable-all",
    ) as HTMLButtonElement;
    expect(enableAllBtn.disabled).toBe(true);
  });

  it("enables all detectors from a partially disabled state", () => {
    const f1 = makeFeature("st_1", "pivot_trendline@1");
    const onFilterChange = vi.fn<(next: StructureFilter) => void>();
    const filter: StructureFilter = {
      ...defaultStructureFilter,
      enabled: true,
      viewMode: "expert",
      disabledDetectorIds: ["pivot_trendline@1"],
    };

    render(
      <StructurePanel
        structures={[]}
        allStructures={[f1]}
        registeredDetectors={["pivot_trendline@1"]}
        activeTimeframe="1m"
        filter={filter}
        onFilterChange={onFilterChange}
        precision={2}
        ignoredCount={0}
        onIgnore={vi.fn()}
        ignoringId={null}
        onAdopt={vi.fn()}
        adoptingId={null}
        adoptedIds={new Set()}
        onAttachAlert={vi.fn()}
        loading={false}
        error={null}
        readOnly={false}
      />,
    );

    const v1Btn = screen.getByTestId("structure-detector-pivot_trendline@1");
    expect(v1Btn.className).toContain("is-unplugged");

    fireEvent.click(screen.getByTestId("structure-detectors-enable-all"));
    expect(onFilterChange).toHaveBeenLastCalledWith({
      ...filter,
      disabledDetectorIds: [],
    });
  });
});
