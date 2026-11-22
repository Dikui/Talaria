import { readFileSync } from "node:fs";
import { expect, it } from "vitest";

// alerts-and-scanning/02: expanding the rule editor must preserve room for the chart panes.
it("gives the chart stage a 480px minimum height while the chart rule editor is expanded", () => {
  const styles = readFileSync(new URL("../../src/styles.css", import.meta.url), "utf8");
  expect(styles).toMatch(
    /\.chart-workbench:has\(\.rule-editor-content:not\(\[hidden\]\)\) \.chart-stage\s*\{[^}]*min-height:\s*480px;/,
  );
});
