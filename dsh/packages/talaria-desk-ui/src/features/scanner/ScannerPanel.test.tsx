import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import type { ScannerTrigger } from "@talaria/contracts";

import { ScannerCard, ScannerPanel } from "./ScannerPanel";
import type { ScannerController } from "./useScanner";

const NOW = 1_000_060_000;

function trigger(overrides: Partial<ScannerTrigger> = {}): ScannerTrigger {
  return {
    trigger_id: "scn_1",
    instrument_id: "BTC-USDT-PERP",
    timeframe: "1m",
    kind: "LIQUIDITY_ANOMALY",
    severity: "WARNING",
    title: "流动性异常",
    summary: "成交额较近 60 根均值放大 3.2σ（分位 99%，倍数 3.10×）",
    evidence: [{ metric: "zscore", value: "3.2" }],
    tags: ["流动性", "1m", "z=3.2"],
    created_ts: NOW - 60_000,
    snapshot_id: "snap_1",
    route: "LOCAL",
    ...overrides,
  };
}

function controller(overrides: Partial<ScannerController> = {}): ScannerController {
  return {
    triggers: [],
    focus: {
      group_id: "scanner-focus",
      name: "扫描焦点",
      items: [],
      instrument_ids: [],
      generated_ts: NOW,
    },
    loading: false,
    scanning: false,
    error: null,
    unread: 0,
    refresh: async () => {},
    scanNow: async () => {},
    ingest: () => {},
    markSeen: () => {},
    clearError: () => {},
    ...overrides,
  };
}

function panel(overrides: Partial<ScannerController> = {}): string {
  return renderToStaticMarkup(
    <ScannerPanel
      controller={controller(overrides)}
      open
      onToggle={() => {}}
      onLocate={() => {}}
      now={NOW}
    />,
  );
}

describe("scanner panel", () => {
  it("renders a card with kind, severity, summary, tags and trigger time", () => {
    const html = panel({ triggers: [trigger()] });
    expect(html).toContain('data-testid="scanner-card"');
    expect(html).toContain("流动性异常");
    expect(html).toContain("关注");
    expect(html).toContain("成交额较近 60 根均值放大 3.2σ");
    expect(html).toContain("z=3.2");
    expect(html).toContain("<time");
    expect(html).toContain("1 分钟前");
    expect(html).not.toContain("展开摘要");
  });

  it("clamps a long multi-indicator summary and offers expand", () => {
    const html = panel({
      triggers: [
        trigger({
          summary:
            "成交量 4.8σ、真实波幅分位 99%、资金费率 99 分位、OI 与价格反向，且价格触及高强度前高 112345.6；多个指标同时异常，需要展开查看完整解释。",
          tags: ["流动性", "波动率", "资金费率", "OI", "关键位"],
        }),
      ],
    });
    expect(html).toContain("scanner-summary is-clamped");
    expect(html).toContain("展开摘要");
    expect(html).toContain("…");
  });

  it("shows a calm empty state and hides the manual scan entry in read-only", () => {
    const html = panel();
    expect(html).toContain('data-testid="scanner-empty"');
    expect(html).toContain("盘面平静，暂无结构性异动");
    expect(html).toContain("手动扫描");

    const readOnly = renderToStaticMarkup(
      <ScannerPanel
        controller={controller()}
        open
        onToggle={() => {}}
        onLocate={() => {}}
        now={NOW}
        readOnly
      />,
    );
    expect(readOnly).not.toContain("手动扫描");
  });

  it("renders focus chips for SCANNER_FOCUS instruments", () => {
    const html = panel({
      focus: {
        group_id: "scanner-focus",
        name: "扫描焦点",
        items: [
          {
            instrument_id: "ETH-USDT-PERP",
            last_trigger_ts: NOW,
            trigger_count: 2,
            severity: "CRITICAL",
            last_title: "OI 背离",
            last_timeframe: "5m",
          },
        ],
        instrument_ids: ["ETH-USDT-PERP"],
        generated_ts: NOW,
      },
    });
    expect(html).toContain("scanner-focus-chip");
    expect(html).toContain("ETH");
    expect(html).toContain("2");
  });

  it("keeps the card click payload testable through ScannerCard props", () => {
    const html = renderToStaticMarkup(
      <ScannerCard
        trigger={trigger()}
        onLocate={() => {
          throw new Error("server render must not invoke locate");
        }}
      />,
    );
    expect(html).toContain("定位到触发快照");
  });
});
