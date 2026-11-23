import { RULE_EDITOR_SCHEMA as schema } from "@talaria/contracts";
import type { DrawingRecord, KeyLevel, RuleIndicator, RuleIndicatorParams } from "@talaria/contracts";
import {
  alertLevelEventLabels,
  barFieldLabel,
  drawingKindLabel,
  indicatorNameLabel,
  metricLabels,
} from "../alerts/model";
import { levelLabel } from "../key-levels/model";
import { type IndicatorDefaults } from "../indicators/config";
import { t, useLocale } from "../../lib/i18n";
import {
  indicatorOperand, isGroup, newComparison, newOperand, nodeCount, operatorParams,
  validDecimal, validDeviation, validPeriod, validRatio,
  type Expression, type Operand, type OperatorName, type Timing,
} from "./ruleForm";

function expressionLabels(): Record<string, string> {
  return {
    compare: t("比较", "Compare"),
    all: t("全部满足（且）", "All of (AND)"),
    any: t("任一满足（或）", "Any of (OR)"),
    not: t("取反（非）", "Not"),
    overlay: t("画线事件", "Drawing event"),
    level_event: t("关键位事件", "Key level event"),
  };
}
function operandLabels(): Record<string, string> {
  return {
    bar: t("K 线字段", "Bar field"),
    event: t("当前行情", "Current market"),
    derivative: t("衍生品读数", "Derivative reading"),
    constant: t("常量", "Constant"),
    indicator: t("指标算子", "Indicator"),
    drawing_price: t("画线价格", "Drawing price"),
    level_price: t("关键位价格", "Key level price"),
  };
}
function comparisonLabels(): Record<string, string> {
  return {
    lt: t("小于", "Less than"),
    lte: t("小于等于", "Less than or equal"),
    gt: t("大于", "Greater than"),
    gte: t("大于等于", "Greater than or equal"),
    eq: t("等于", "Equal"),
    cross_above: t("上穿", "Crosses above"),
    cross_below: t("下穿", "Crosses below"),
  };
}
function edgeLabels(): Record<string, string> {
  return { lower: t("下沿", "Lower"), upper: t("上沿", "Upper") };
}
const operators = Object.keys(schema.operators) as OperatorName[];

interface ChoiceProps<T extends string> {
  label: string;
  value: T;
  options: readonly T[];
  labels?: Readonly<Record<string, string | undefined>>;
  placeholder?: string;
  onChange: (value: T) => void;
}

function Choice<T extends string>({ label, value, options, labels, placeholder, onChange }: ChoiceProps<T>) {
  useLocale();
  const selected = options.includes(value);
  return (
    <label className="rule-field">
      <span>{label}</span>
      <select value={selected ? value : ""} aria-invalid={!selected}
        onChange={(event) => onChange(event.currentTarget.value as T)}>
        {placeholder && <option value="" disabled>{placeholder}</option>}
        {options.map((option) => <option key={option} value={option}>{labels?.[option] ?? option}</option>)}
      </select>
    </label>
  );
}

function DecimalField({ label, value, invalid, placeholder, onChange }: {
  label: string;
  value: string;
  invalid: boolean;
  placeholder?: string;
  onChange: (value: string) => void;
}) {
  useLocale();
  return (
    <label className="rule-field">
      <span>{label}</span>
      <input type="text" inputMode="decimal" value={value} placeholder={placeholder} aria-invalid={invalid}
        onChange={(event) => onChange(event.currentTarget.value.trim())} />
    </label>
  );
}

interface FormContext {
  timing: Timing;
  defaults: IndicatorDefaults;
  drawings: readonly DrawingRecord[];
  levels: readonly KeyLevel[];
  pricePrecision?: number;
}

function DrawingChoice({ id, drawings, ratio, onSelect, onRatio }: {
  id: string;
  drawings: FormContext["drawings"];
  ratio?: string | null;
  onSelect: (id: string) => void;
  onRatio: (ratio: string | null) => void;
}) {
  useLocale();
  const options = drawings.map(({ drawing_id }) => drawing_id);
  return (
    <>
      <Choice label={t("画线", "Drawing")} value={id} options={options}
        labels={Object.fromEntries(drawings.map((drawing) => [
          drawing.drawing_id, `${drawingKindLabel(drawing.kind)} · ${drawing.drawing_id.slice(4, 10)}`,
        ]))}
        placeholder={options.length ? t("选择画线", "Select a drawing") : t("当前图表没有可用画线", "No drawings available on this chart")} onChange={onSelect} />
      {drawings.find((drawing) => drawing.drawing_id === id)?.kind === "FIB_RETRACEMENT" && (
        <DecimalField label={t("回撤比例（0–1）", "Retracement ratio (0–1)")} value={ratio ?? ""} placeholder="0.5"
          invalid={!validRatio(ratio)} onChange={(value) => onRatio(value || null)} />
      )}
    </>
  );
}

function LevelChoice({ id, context, onSelect }: {
  id: string;
  context: FormContext;
  onSelect: (level: KeyLevel) => void;
}) {
  useLocale();
  const levels = new Map(context.levels.map((level) => [level.level_id, level]));
  return (
    <Choice label={t("关键位", "Key level")} value={id} options={id && !levels.has(id) ? [id, ...levels.keys()] : [...levels.keys()]}
      labels={Object.fromEntries(context.levels.map((level) => [
        level.level_id, levelLabel(level, context.pricePrecision ?? 2),
      ]))}
      placeholder={levels.size ? t("选择关键位", "Select a key level") : t("当前图表没有可用关键位", "No key levels available on this chart")}
      onChange={(value) => {
        const level = levels.get(value);
        if (level) onSelect(level);
      }} />
  );
}

function IndicatorFields({ operand, defaults, onChange }: {
  operand: RuleIndicator;
  defaults: IndicatorDefaults;
  onChange: (operand: RuleIndicator) => void;
}) {
  useLocale();
  const operatorLabels = Object.fromEntries(operators.map((name) => [name, indicatorNameLabel(name)]));
  const params = operatorParams(operand.name);
  const channels = schema.operators[operand.name].channels;
  const setParams = (patch: RuleIndicatorParams) => onChange({
    ...operand, params: { ...operand.params, ...patch },
  });
  return (
    <>
      <Choice label={t("算子", "Indicator")} value={operand.name} options={operators} labels={operatorLabels}
        onChange={(name) => onChange(indicatorOperand(name, defaults))} />
      {(operand.window ?? "alert") === "alert" && operand.params?.anchor !== "session" && (
        <span>{t(`预警窗口 · 最近 ${Math.max(600, (operand.params?.period ?? 1) + 2)} 根`, `Alert window · last ${Math.max(600, (operand.params?.period ?? 1) + 2)} bars`)}</span>
      )}
      {channels.length > 1 && (
        <Choice label={t("输出通道", "Output channel")} value={operand.channel ?? channels[0]} options={channels}
          labels={{ mid: t("中轨", "Middle"), upper: t("上轨", "Upper"), lower: t("下轨", "Lower") }}
          onChange={(channel) => onChange({ ...operand, channel })} />
      )}
      <label className="rule-field">
        <span>{t(
          `周期（${params.period.minimum}–${params.period.maximum}）`,
          `Period (${params.period.minimum}–${params.period.maximum})`,
        )}</span>
        <input type="number" min={params.period.minimum} max={params.period.maximum} step={1}
          value={operand.params?.period ?? ""} aria-invalid={!validPeriod(operand.name, operand.params?.period)}
          onChange={(event) => {
            const period = event.currentTarget.valueAsNumber;
            setParams({ period: Number.isNaN(period) ? null : period });
          }} />
      </label>
      {"std_dev" in params && (
        <DecimalField label={t("标准差倍数（> 0）", "Std-dev multiplier (> 0)")} value={operand.params?.std_dev ?? ""}
          invalid={!validDeviation(operand.params?.std_dev ?? "")}
          onChange={(std_dev) => setParams({ std_dev })} />
      )}
      {"anchor" in params && (
        <Choice label={t("锚点", "Anchor")} value={operand.params?.anchor ?? params.anchor.default}
          options={params.anchor.enum} labels={{
            snapshot: operand.window === "chart" ? t("快照起点", "Snapshot start") : t("预警窗口起点", "Alert window start"),
            session: t("UTC 日界", "UTC day boundary"),
          }}
          onChange={(anchor) => setParams({ anchor })} />
      )}
    </>
  );
}

function OperandFields({ label, operand, context, onChange }: {
  label: string;
  operand: Operand;
  context: FormContext;
  onChange: (operand: Operand) => void;
}) {
  useLocale();
  const options = schema.operands.filter((source) => {
    if (context.timing === "bar_close") return source !== "event";
    if (operand.source === "event") return source === "event";
    return source !== "event" && source !== "bar" && source !== "derivative";
  });
  return (
    <div className="rule-operand">
      <Choice label={label} value={operand.source} options={options} labels={operandLabels()}
        onChange={(source) => onChange(newOperand(source, context.defaults))} />
      {operand.source === "constant" && (
        <DecimalField label={t("数值", "Value")} value={operand.value} invalid={!validDecimal(operand.value)}
          onChange={(value) => onChange({ ...operand, value })} />
      )}
      {operand.source === "bar" && (
        <Choice label={t("字段", "Field")} value={operand.field} options={schema.barFields} labels={Object.fromEntries(schema.barFields.map((field) => [field, barFieldLabel(field)]))}
          onChange={(field) => onChange({ ...operand, field })} />
      )}
      {operand.source === "event" && (
        <Choice label={t("行情字段", "Market field")} value={operand.metric} options={schema.eventMetrics} labels={metricLabels()}
          onChange={(metric) => onChange({ ...operand, metric })} />
      )}
      {operand.source === "derivative" && (
        <Choice label={t("衍生品字段", "Derivative field")} value={operand.kind} options={schema.derivatives} labels={metricLabels()}
          onChange={(kind) => onChange({ ...operand, kind })} />
      )}
      {operand.source === "indicator" && <IndicatorFields operand={operand} defaults={context.defaults} onChange={onChange} />}
      {operand.source === "drawing_price" && (
        <DrawingChoice id={operand.drawing_id} drawings={context.drawings} ratio={operand.ratio}
          onSelect={(drawing_id) => onChange({ ...operand, drawing_id, ratio: null })}
          onRatio={(ratio) => onChange({ ...operand, ratio })} />
      )}
      {operand.source === "level_price" && (
        <LevelChoice id={operand.level_id} context={context}
          onSelect={(level) => onChange({ ...operand, level_id: level.level_id, level_kind: level.kind })} />
      )}
      {(operand.source === "drawing_price" || operand.source === "level_price") && (
        <Choice label={t("边界", "Edge")} value={operand.edge ?? "upper"} options={schema.edges} labels={edgeLabels()}
          onChange={(edge) => onChange({ ...operand, edge })} />
      )}
    </div>
  );
}

interface NodeFieldsProps {
  node: Expression;
  context: FormContext;
  depth: number;
  canAdd: boolean;
  onChange: (node: Expression) => void;
  onRemove?: () => void;
}

function NodeFields({ node, context, depth, canAdd, onChange, onRemove }: NodeFieldsProps) {
  useLocale();
  const expressionLabelsHere = expressionLabels();
  const intrabar = context.timing === "event";
  const group = isGroup(node);
  const canNest = depth < schema.maxDepth;
  const types = schema.expressions.filter((op) => {
    if (op === "not" && group && node.args.length > 1) return false;
    if (op === "all" || op === "any" || op === "not") return group || (canNest && canAdd);
    return true;
  });

  function changeType(op: Expression["op"]) {
    if (op === node.op) return;
    switch (op) {
      case "all": case "any": case "not":
        onChange({ op, args: group ? node.args : [node] });
        break;
      case "compare":
        onChange(group && node.args.length === 1 && node.args[0].op === "compare"
          ? node.args[0] : newComparison(context.timing));
        break;
      case "overlay":
        onChange({ op, drawing_id: "", mode: intrabar ? "TOUCH" : "CLOSE_BREAK", direction: "ABOVE" });
        break;
      case "level_event":
        onChange({ op, level_id: "", event: "TOUCH" });
        break;
    }
  }

  return (
    <fieldset className="rule-node">
      <legend className="rule-sr-only">{expressionLabelsHere[node.op]}</legend>
      <div className="rule-node-header">
        <Choice label={t("条件类型", "Condition type")} value={node.op} options={types} labels={expressionLabelsHere} onChange={changeType} />
        {onRemove && <button type="button" className="drawing-action" onClick={onRemove}>{t("移除", "Remove")}</button>}
      </div>
      {group && (
        <div className="rule-children">
          {node.args.map((child, index) => (
            <NodeFields key={index} node={child} context={context} depth={depth + 1} canAdd={canAdd}
              onChange={(next) => onChange({ ...node, args: node.args.map((arg, i) => i === index ? next : arg) })}
              onRemove={node.args.length > 1
                ? () => onChange({ ...node, args: node.args.filter((_, i) => i !== index) })
                : undefined} />
          ))}
          {node.op !== "not" && canNest && (
            <button type="button" className="drawing-action" disabled={!canAdd}
              onClick={() => onChange({ ...node, args: [...node.args, newComparison(context.timing)] })}>{t("添加条件", "Add condition")}</button>
          )}
        </div>
      )}
      {node.op === "compare" && (
        <div className="rule-comparison">
          <OperandFields label={t("左侧", "Left")} operand={node.left} context={context} onChange={(left) => onChange({ ...node, left })} />
          <Choice label={t("比较", "Comparison")} value={node.test}
            options={schema.comparisons.filter((test) => !intrabar || !test.startsWith("cross_"))}
            labels={comparisonLabels()} onChange={(test) => onChange({ ...node, test })} />
          <OperandFields label={t("右侧", "Right")} operand={node.right} context={context} onChange={(right) => onChange({ ...node, right })} />
        </div>
      )}
      {node.op === "overlay" && (
        <div className="rule-reference">
          <DrawingChoice id={node.drawing_id} drawings={context.drawings} ratio={node.ratio}
            onSelect={(drawing_id) => onChange({ ...node, drawing_id, ratio: null })}
            onRatio={(ratio) => onChange({ ...node, ratio })} />
          <Choice label={t("画线事件", "Drawing event")} value={node.mode}
            options={intrabar ? schema.overlayModes.filter((mode) => mode === "TOUCH") : schema.overlayModes}
            labels={{ TOUCH: t("触及", "Touch"), CLOSE_BREAK: t("收盘突破", "Close break") }} onChange={(mode) => onChange({ ...node, mode })} />
          <Choice label={t("方向", "Direction")} value={node.direction} options={schema.directions}
            labels={{ ABOVE: t("向上", "Up"), BELOW: t("向下", "Down") }} onChange={(direction) => onChange({ ...node, direction })} />
        </div>
      )}
      {node.op === "level_event" && (
        <div className="rule-reference">
          <LevelChoice id={node.level_id} context={context}
            onSelect={(level) => onChange({ ...node, level_id: level.level_id, level_kind: level.kind })} />
          <Choice label={t("关键位事件", "Key level event")} value={node.event}
            options={intrabar ? schema.levelEvents.filter((event) => event === "TOUCH") : schema.levelEvents}
            labels={alertLevelEventLabels()} onChange={(event) => onChange({ ...node, event })} />
        </div>
      )}
    </fieldset>
  );
}

export function RuleEditorForm({ expression, onChange, ...context }: FormContext & {
  expression: Expression | null;
  onChange: (expression: Expression | null) => void;
}) {
  useLocale();
  if (expression === null) {
    return (
      <div className="rule-empty">
        <p>{t("添加条件后，图表会自动预览规则。", "Add a condition and the chart previews the rule automatically.")}</p>
        <button type="button" className="drawing-action" onClick={() => onChange(newComparison(context.timing))}>
          {t("添加条件", "Add condition")}
        </button>
      </div>
    );
  }
  return (
    <NodeFields node={expression} context={context} depth={1} canAdd={nodeCount(expression) < schema.maxNodes}
      onChange={onChange} onRemove={() => onChange(null)} />
  );
}

export function RuleTiming({ timing, onChange }: { timing: Timing; onChange: (timing: Timing) => void }) {
  useLocale();
  return (
    <Choice label={t("求值时机", "Evaluation timing")} value={timing} options={schema.timings}
      labels={{ bar_close: t("K 线收盘", "Bar close"), event: t("盘中行情事件", "Intraday market event") }} onChange={onChange} />
  );
}
