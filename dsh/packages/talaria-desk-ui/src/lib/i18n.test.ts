import { afterEach, expect, it } from "vitest";
import { getLocale, normalizeLocale, setLocale, t } from "./i18n";

// small/spec: the workbench follows the host language without changing panel identity.
afterEach(() => setLocale("zh"));
it("switches existing workbench text between regional Chinese and English locales", () => {
  setLocale("en-US");
  expect(t("扫描", "Scanner")).toBe("Scanner");
  setLocale("zh-Hant-TW");
  expect(getLocale()).toBe("zh");
  expect(t("扫描", "Scanner")).toBe("扫描");
  expect(normalizeLocale("fr")).toBe("en");
});
