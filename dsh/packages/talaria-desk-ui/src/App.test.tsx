import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { expect, it, vi } from "vitest";
import { ChartWorkbench } from "./App";
import { setLocale } from "./lib/i18n";
import { createLink, type PanelLink } from "./workbench/link";

// Keep the browser-only KLineChart library out of this server render.
vi.mock("./features/market-chart/MarketChart", () => ({ MarketChart: () => null }));

// Read the same link snapshot during SSR without subscribing to browser updates.
vi.mock("./workbench/usePanelLink", () => ({
  useLinkSnapshot: (link: PanelLink) => link.getSnapshot(),
}));

// alerts-and-scanning/02: absolute chart overlays must stay below the rule editor.
it("renders the chart rule editor directly above the chart panel as a sibling", () => {
  const link = createLink({
    key: "chart-rule-layout",
    panel: "chart",
    transport: { send: () => {}, subscribe: () => () => {} },
  });

  try {
    const html = renderToStaticMarkup(createElement(ChartWorkbench, {
      embedded: false, fullWorkbench: false, link, theme: "light",
    }));
    const sections = html.match(/<\/?section\b[^>]*>/g)?.join("");
    expect(sections).toMatch(
      /aria-label="图表规则编辑器"><\/section><section class="chart-panel[^"]*" aria-label="K 线主图">/,
    );
  } finally {
    link.close();
  }
});

it("freezes current chart evidence when starting a manual draft", async () => {
  const { newManualDraft } = await import("./features/paper/DraftForm");
  const freeze = vi.fn().mockResolvedValue({ instrument_id: "BTC", snapshot_id: "new" });
  expect(await newManualDraft("BTC", "1h", freeze)).toEqual({ instrument_id: "BTC", snapshot_id: "new" });
  expect(freeze).toHaveBeenCalledWith("BTC", ["1h"]);
});

it("renders the workbench chrome in English after a runtime locale switch", () => {
  const link = createLink({
    key: "chart-english",
    panel: "chart",
    transport: { send: () => {}, subscribe: () => () => {} },
  });
  setLocale("en");
  try {
    const html = renderToStaticMarkup(createElement(ChartWorkbench, {
      embedded: false, fullWorkbench: false, link, theme: "light",
    }));
    expect(html).toContain('aria-label="Candlestick chart"');
    expect(html).toContain("Talaria Chart");
  } finally {
    setLocale("zh");
    link.close();
  }
});
