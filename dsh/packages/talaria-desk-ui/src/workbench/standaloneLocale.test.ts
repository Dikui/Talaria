import { expect, it } from "vitest";
import { PANELS, panelFromPath, panelLabel, panelPath } from "./panels";
import { deskHref, standaloneLocale } from "./standaloneLocale";

// small/regression: previously bookmarked desk routes still open the requested tool.
it.each([
  ["chart", "Chart"], ["alerts", "Alerts"], ["scanner", "Scanner"],
  ["levels", "Key Levels"], ["accounts", "Accounts"],
] as const)("keeps the %s deep link when following an English desk link", (panel, label) => {
  const href = deskHref(panelPath(panel), "?lang=en&unused=1");
  expect(href).toBe(`/desk/${panel}?lang=en`);
  expect(panelFromPath(new URL(href, "https://example.test").pathname)).toBe(panel);
  expect(panelLabel(panel, "en")).toBe(label);
});

// small/spec: standalone has no host preference; explicit links override the browser.
it("uses the browser language unless a supported link override is present", () => {
  expect(standaloneLocale("", "zh-Hans")).toBe("zh");
  expect(standaloneLocale("?lang=en-GB", "zh-CN")).toBe("en");
  expect(standaloneLocale("?lang=wrong", "zh-CN")).toBe("zh");
  expect(deskHref("/desk", "?unused=1")).toBe("/desk");
  expect(panelFromPath("/desk/link")).toBeUndefined();
  expect(PANELS).toHaveLength(5);
});
