import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, expect, it, vi } from "vitest";
import { setLocale } from "../lib/i18n";
import { createLink, type PanelLink } from "./link";
import { PANELS, type PanelName } from "./panels";
import { WorkbenchPanel } from "./WorkbenchPanel";

// Render the actual controls for each tab without opening a browser, socket or backend.
vi.mock("../features/market-chart/MarketChart", () => ({ MarketChart: () => null }));
let link: PanelLink;
vi.mock("./usePanelLink", () => ({
  usePanelLink: () => link,
  useLinkSnapshot: (active: PanelLink) => active.getSnapshot(),
}));
afterEach(() => { link.close(); setLocale("zh"); });

it.each(PANELS)("renders the %s tab in English and Chinese without changing the panel id", (panel: PanelName) => {
  link = createLink({ panel, key: panel, transport: { send() {}, subscribe: () => () => {} } });
  const render = () => renderToStaticMarkup(<WorkbenchPanel panel={panel} theme="light" persistenceKey={panel} />);
  setLocale("en");
  const english = render();
  expect(english).toContain("<h1");
  expect(english.match(/[^<>"]{0,12}\p{Script=Han}[^<>"]{0,20}/gu)).toBeNull();
  setLocale("zh");
  expect(render()).toMatch(/\p{Script=Han}/u);
});
