import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import type { Rule } from "@talaria/contracts";
import { afterEach, expect, it } from "vitest";
import { setLocale } from "../../lib/i18n";
import { INDICATOR_DEFAULTS } from "../indicators/config";
import { RuleEditorForm } from "./RuleEditor";

afterEach(() => setLocale("zh"));

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

// alerts-and-scanning/02: nested rules remain editable, including operator-specific fields.
it("renders a three-level rule as structured controls with operator-specific parameters", () => {
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

it("renders focusable hit details with separate time, price and kind for observations and triggers", async () => {
  const { RuleHits } = await import("./ChartRuleEditor");
  const match = { timestamp: 60_000, observed_at: 120_037, price: "102.25" };
  const html = renderToStaticMarkup(createElement(RuleHits, { conditions: [match], triggers: [match] }));
  expect(html).toContain("条件成立");
  expect(html).toContain("触发预警");
  expect(html).toContain("102.25");
  expect(html).toContain("1970-01-01T00:02:00.037Z");
  expect(html).toMatch(/<li[^>]*tabIndex="0"/i);
});

it("renders form options and hit details in English after a locale switch", async () => {
  setLocale("en-US");
  const html = renderToStaticMarkup(createElement(RuleEditorForm, {
    timing: "bar_close", expression, onChange: () => {},
    defaults: INDICATOR_DEFAULTS, drawings: [], levels: [],
  }));
  expect(html).toContain(">Crosses above<");
  expect(html).toContain(">Upper<");
  expect(html).toContain("Period (2–5000)");
  expect(html).not.toMatch(/[\u4e00-\u9fff]/);

  const { RuleHits } = await import("./ChartRuleEditor");
  const match = { timestamp: 60_000, observed_at: 120_037, price: "102.25" };
  const hits = renderToStaticMarkup(createElement(RuleHits, { conditions: [match], triggers: [match] }));
  expect(hits).toContain("Matched details");
  expect(hits).toContain("Conditions met");
  expect(hits).toContain("Observed");
});
