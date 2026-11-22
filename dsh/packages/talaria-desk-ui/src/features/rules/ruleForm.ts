import { RULE_EDITOR_SCHEMA as schema } from "@talaria/contracts";
import type { IndicatorName, Rule, RuleComparison, RuleIndicator } from "@talaria/contracts";
import type { IndicatorDefaults } from "../indicators/config";

export type Timing = Rule["timing"];
export type Expression = Rule["expression"];
export type Operand = RuleComparison["left"];
export type OperatorName = RuleIndicator["name"];

export function isGroup(node: Expression): node is Extract<Expression, { args: Expression[] }> {
  return "args" in node;
}

export function nodeCount(node: Expression): number {
  return isGroup(node) ? 1 + node.args.reduce((count, child) => count + nodeCount(child), 0) : 1;
}

export function operatorName(name: OperatorName): IndicatorName {
  return name.replace(/^legacy_/, "").toUpperCase() as IndicatorName;
}

export function operatorParams(name: OperatorName) {
  return schema.operators[name].parameters;
}

export function indicatorOperand(name: OperatorName, defaults: IndicatorDefaults): RuleIndicator {
  const indicator = operatorName(name);
  const values = defaults[indicator];
  const parameters = operatorParams(name);
  return {
    source: "indicator", name,
    version: schema.operators[name].version,
    channel: schema.operators[name].channels[0],
    params: {
      period: values.period,
      ...("std_dev" in parameters ? { std_dev: values.stdDev } : {}),
      ...("anchor" in parameters ? { anchor: values.anchor } : {}),
    },
  };
}

export function newOperand(source: Operand["source"], defaults: IndicatorDefaults): Operand {
  switch (source) {
    case "bar": return { source, field: "close" };
    case "event": return { source, metric: "last_price" };
    case "constant": return { source, value: "" };
    case "derivative": return { source, kind: "funding_rate" };
    case "indicator": return indicatorOperand("EMA", defaults);
    case "drawing_price": return { source, drawing_id: "", edge: "upper" };
    case "level_price": return { source, level_id: "", edge: "upper" };
  }
}

export function newComparison(timing: Timing): RuleComparison {
  return {
    op: "compare", test: "gt",
    left: timing === "event" ? { source: "event", metric: "last_price" } : { source: "bar", field: "close" },
    right: { source: "constant", value: "" },
  };
}

const decimalPattern = new RegExp(schema.decimalPattern);
export const validDecimal = (value: string) => decimalPattern.test(value);
export const validDeviation = (value: string) => validDecimal(value) && !value.startsWith("-") && /[1-9]/.test(value);
export const validRatio = (value: string | null | undefined) =>
  value == null || /^(?:0(?:\.\d+)?|-0(?:\.0+)?|1(?:\.0+)?)$/.test(value);
export function validPeriod(name: OperatorName, period: number | null | undefined): boolean {
  const { minimum, maximum } = operatorParams(name).period;
  return period != null && Number.isInteger(period) && period >= minimum && period <= maximum;
}

export function expressionComplete(
  node: Expression, drawingIds: ReadonlySet<string>, levelIds: ReadonlySet<string>,
): boolean {
  function operandComplete(operand: Operand): boolean {
    switch (operand.source) {
      case "constant": return validDecimal(operand.value);
      case "indicator":
        return validPeriod(operand.name, operand.params?.period) &&
          (!("std_dev" in operatorParams(operand.name)) || validDeviation(operand.params?.std_dev ?? ""));
      case "drawing_price": return drawingIds.has(operand.drawing_id) && validRatio(operand.ratio);
      case "level_price": return levelIds.has(operand.level_id);
      default: return true;
    }
  }
  if (isGroup(node)) return node.args.every((child) => expressionComplete(child, drawingIds, levelIds));
  switch (node.op) {
    case "compare": return operandComplete(node.left) && operandComplete(node.right);
    case "overlay": return drawingIds.has(node.drawing_id) && validRatio(node.ratio);
    case "level_event": return levelIds.has(node.level_id);
  }
}
