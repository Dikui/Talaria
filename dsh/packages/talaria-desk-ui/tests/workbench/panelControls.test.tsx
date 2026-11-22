import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { expect, it, vi } from "vitest";
import type { LinkGroup, PanelLink } from "../../src/workbench/link";
import { LinkControls } from "../../src/workbench/PanelControls";

vi.mock("../../src/workbench/usePanelLink", () => ({
  useLinkSnapshot: (link: PanelLink) => link.getSnapshot(),
}));

// 平台 02 审查：用同一组数据统计并展开窗口列表。
it.each([
  ["red", 1, true],
  ["none", 0, false],
] as const)("lists only the counted windows when the group is %s", (group: LinkGroup, count, showsChart) => {
  const link = {
    getSnapshot: () => ({
      group,
      peers: [
        { from: "a", group: "red", panel: "chart", instrumentId: "SOL-USDT-PERP", timeframe: "5m" },
        { from: "b", group: "blue", panel: "alerts", instrumentId: "ETH-USDT-PERP", timeframe: "15m" },
        { from: "c", group: "none", panel: "levels", instrumentId: "BTC-USDT-PERP", timeframe: "1h" },
      ],
    }),
  } as unknown as PanelLink;
  const html = renderToStaticMarkup(createElement(LinkControls, { link }));
  expect(html).toContain(`<summary>同组 ${count}</summary>`);
  expect(html).not.toContain("ETH-USDT-PERP");
  expect(html).not.toContain("BTC-USDT-PERP");
  expect(html.includes("SOL-USDT-PERP")).toBe(showsChart);
  expect(html.includes("暂无其他面板")).toBe(!showsChart);
});
