import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import type { ScannerBriefing } from "@talaria/contracts";

import { BriefingInbox } from "./BriefingInbox";
import type { BriefingsController } from "./useBriefings";

const NOW = 1_795_023_000_000;

function briefing(overrides: Partial<ScannerBriefing> = {}): ScannerBriefing {
  return {
    id: "brf_1",
    trigger_id: "scn_1",
    snapshot_id: "snap_1234567890",
    instrument_id: "BTC-USDT-PERP",
    timeframe: "1m",
    status: "READY",
    summary: {
      trigger_reason: "成交额较近 60 根均值放大 3.2σ，价格突破前高",
      key_levels: [{ label: "前高", price: "112345.6", note: "观察承接" }],
      risks: ["追高风险", "快照后行情可能已变化"],
    },
    evidence_refs: [
      {
        evidence_id: "snapshot:snap_1234567890",
        kind: "SNAPSHOT",
        label: "冻结快照",
        detail: "1m · 300 根",
      },
    ],
    abstain_reason: null,
    created_ts: NOW - 60_000,
    read_ts: null,
    ...overrides,
  };
}

function controller(
  overrides: Partial<BriefingsController> = {},
): BriefingsController {
  return {
    briefings: [],
    unread: 0,
    loading: false,
    error: null,
    refresh: async () => {},
    markRead: async () => {},
    consult: async () => null,
    clearError: () => {},
    ...overrides,
  };
}

function render(
  overrides: Partial<BriefingsController> = {},
  readOnly = false,
): string {
  return renderToStaticMarkup(
    <BriefingInbox
      controller={controller(overrides)}
      open
      onClose={() => {}}
      onLocate={() => {}}
      onBringToCollab={() => {}}
      readOnly={readOnly}
      now={NOW}
    />,
  );
}

describe("briefing inbox", () => {
  it("renders a ready briefing with key levels, risks and folded evidence", () => {
    const html = render({ briefings: [briefing()], unread: 1 });
    expect(html).toContain('data-testid="briefing-card"');
    expect(html).toContain("成交额较近 60 根均值放大");
    expect(html).toContain("前高");
    expect(html).toContain("追高风险");
    expect(html).toContain('data-testid="briefing-evidence"');
    expect(html).toContain("证据引用 · 1");
    expect(html).toContain("1 条未读");
    expect(html).toContain("带入协同");
  });

  it("collapses a long summary and keeps the expand control", () => {
    const html = render({
      briefings: [
        briefing({
          summary: {
            trigger_reason: "成交额放大。".repeat(40),
            key_levels: [],
            risks: [],
          },
        }),
      ],
      unread: 1,
    });
    expect(html).toContain("briefing-reason is-clamped");
    expect(html).toContain("展开摘要");
    expect(html).toContain("…");
  });

  it("shows an abstain card without a consultation action", () => {
    const html = render({
      briefings: [
        briefing({
          status: "ABSTAIN",
          summary: null,
          evidence_refs: [],
          abstain_reason: "HOST_OFFLINE",
        }),
      ],
      unread: 1,
    });
    expect(html).toContain('data-testid="briefing-abstain"');
    expect(html).toContain("Abstain");
    expect(html).toContain("协同宿主离线");
    expect(html).not.toContain("带入协同");
  });

  it("renders a calm empty state and hides the bring action on read-only", () => {
    const empty = render();
    expect(empty).toContain('data-testid="briefings-empty"');
    expect(empty).toContain("暂无协同简报");

    const narrow = render({ briefings: [briefing()], unread: 1 }, true);
    expect(narrow).not.toContain("带入协同");
    expect(narrow).toContain("切到主图");
  });

  it("renders nothing when the inbox is closed", () => {
    const html = renderToStaticMarkup(
      <BriefingInbox
        controller={controller()}
        open={false}
        onClose={() => {}}
        onLocate={() => {}}
        onBringToCollab={() => {}}
      />,
    );
    expect(html).toBe("");
  });
});
