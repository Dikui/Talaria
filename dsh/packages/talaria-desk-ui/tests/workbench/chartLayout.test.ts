import { readFileSync } from "node:fs";
import { expect, it } from "vitest";

const styles = readFileSync(new URL("../../src/styles.css", import.meta.url), "utf8");

// alerts-and-scanning/02, trading/04: a 480px chart stage leaves 320px for the main pane.
it.each([
  ["the chart rule editor", [".rule-editor-content"]],
  ["the paper draft form", [".paper-draft-form"]],
  ["both panels", [".rule-editor-content", ".paper-draft-form"]],
])("preserves the chart height and allows page scrolling with %s open", (_, openPanels) => {
  const layout = styles.match(
    /\.app-shell:has\(> \.workspace :is\(([^)]+)\):not\(\[hidden\]\)\)\s*\{([^}]+)\}/,
  );
  expect(layout?.[1].split(/\s*,\s*/)).toEqual(expect.arrayContaining(openPanels));
  expect(layout?.[2]).toMatch(/--chart-layout-min-height:\s*min-content;/);
  expect(layout?.[2]).toMatch(/--chart-stage-min-height:\s*480px;/);
  expect(styles).toMatch(/\.chart-stage\s*\{[^}]*min-height:\s*var\(--chart-stage-min-height,\s*0px\);/);
});
