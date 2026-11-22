import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import type { Rule } from "@talaria/contracts";
import { expect, it } from "vitest";
import { INDICATOR_DEFAULTS } from "../indicators/config";
import { RuleEditorForm } from "./RuleEditor";

// alerts-and-scanning/02: nested rules remain editable, including operator-specific fields.
it("renders a three-level rule as structured controls with operator-specific parameters", () => {
  const expression: Rule["expression"] = {
    op: "all",
    args: [{
      op: "any",
      args: [{
        op: "compare", test: "cross_above",
        left: { source: "bar", field: "close" },
        right: {
          source: "indicator", name: "BOLL", channel: "upper",
          params: { period: 20, std_dev: "2.25" },
        },
      }],
    }],
  };
  const html = renderToStaticMarkup(createElement(RuleEditorForm, {
    timing: "bar_close", expression, onChange: () => {},
    defaults: INDICATOR_DEFAULTS, drawings: [], levels: [],
  }));
  expect(html.match(/<fieldset/g)).toHaveLength(3);
  expect(html).toContain('<option value="cross_above" selected="">上穿</option>');
  expect(html).toContain('<option value="upper" selected="">上轨</option>');
  expect(html).toContain('value="2.25"');
  expect(html).not.toContain("<textarea");
});
