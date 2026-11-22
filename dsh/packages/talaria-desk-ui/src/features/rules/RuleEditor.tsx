import { RULE_EDITOR_SCHEMA as schema } from "@talaria/contracts";
import type { DrawingRecord, KeyLevel, RuleIndicator, RuleIndicatorParams } from "@talaria/contracts";
import { ALERT_LEVEL_EVENT_LABELS, METRIC_LABELS } from "../alerts/model";
import { KIND_LABELS } from "../drawings/tools";
import { levelLabel } from "../key-levels/model";
import { INDICATOR_LABELS, type IndicatorDefaults } from "../indicators/config";
import {
  indicatorOperand, isGroup, newComparison, newOperand, nodeCount, operatorParams,
  validDecimal, validDeviation, validPeriod, validRatio,
  type Expression, type Operand, type OperatorName, type Timing,
} from "./ruleForm";

const expressionLabels = {
  compare: "比较", all: "全部满足（且）", any: "任一满足（或）", not: "取反（非）",
  overlay: "画线事件", level_event: "关键位事件",
};
const operandLabels = {
  bar: "K 线字段", event: "当前行情", derivative: "衍生品读数", constant: "常量",
  indicator: "指标算子", drawing_price: "画线价格", level_price: "关键位价格",
};
const comparisonLabels = {
  lt: "小于", lte: "小于等于", gt: "大于", gte: "大于等于", eq: "等于",
  cross_above: "上穿", cross_below: "下穿",
};
const barLabels = { open: "开盘价", high: "最高价", low: "最低价", close: "收盘价", volume: "成交量", turnover: "成交额" };
const edgeLabels = { lower: "下沿", upper: "上沿" };
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
  const options = drawings.map(({ drawing_id }) => drawing_id);
  return (
    <>
      <Choice label="画线" value={id} options={options}
        labels={Object.fromEntries(drawings.map((drawing) => [
          drawing.drawing_id, `${KIND_LABELS[drawing.kind]} · ${drawing.drawing_id.slice(4, 10)}`,
        ]))}
        placeholder={options.length ? "选择画线" : "当前图表没有可用画线"} onChange={onSelect} />
      {drawings.find((drawing) => drawing.drawing_id === id)?.kind === "FIB_RETRACEMENT" && (
        <DecimalField label="回撤比例（0–1）" value={ratio ?? ""} placeholder="0.5"
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
  const levels = new Map(context.levels.map((level) => [level.level_id, level]));
  return (
    <Choice label="关键位" value={id} options={id && !levels.has(id) ? [id, ...levels.keys()] : [...levels.keys()]}
      labels={Object.fromEntries(context.levels.map((level) => [
        level.level_id, levelLabel(level, context.pricePrecision ?? 2),
      ]))}
      placeholder={levels.size ? "选择关键位" : "当前图表没有可用关键位"}
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
  const params = operatorParams(operand.name);
  const channels = schema.operators[operand.name].channels;
  const setParams = (patch: RuleIndicatorParams) => onChange({
    ...operand, params: { ...operand.params, ...patch },
  });
  return (
    <>
      <Choice label="算子" value={operand.name} options={operators} labels={INDICATOR_LABELS}
        onChange={(name) => onChange(indicatorOperand(name, defaults))} />
      {(operand.window ?? "alert") === "alert" && operand.params?.anchor !== "session" && (
        <span>预警窗口 · 最近 {Math.max(600, (operand.params?.period ?? 1) + 2)} 根</span>
      )}
      {channels.length > 1 && (
        <Choice label="输出通道" value={operand.channel ?? channels[0]} options={channels}
          labels={{ mid: "中轨", upper: "上轨", lower: "下轨" }}
          onChange={(channel) => onChange({ ...operand, channel })} />
      )}
      <label className="rule-field">
        <span>周期（{params.period.minimum}–{params.period.maximum}）</span>
        <input type="number" min={params.period.minimum} max={params.period.maximum} step={1}
          value={operand.params?.period ?? ""} aria-invalid={!validPeriod(operand.name, operand.params?.period)}
          onChange={(event) => {
            const period = event.currentTarget.valueAsNumber;
            setParams({ period: Number.isNaN(period) ? null : period });
          }} />
      </label>
      {"std_dev" in params && (
        <DecimalField label="标准差倍数（> 0）" value={operand.params?.std_dev ?? ""}
          invalid={!validDeviation(operand.params?.std_dev ?? "")}
          onChange={(std_dev) => setParams({ std_dev })} />
      )}
      {"anchor" in params && (
        <Choice label="锚点" value={operand.params?.anchor ?? params.anchor.default}
          options={params.anchor.enum} labels={{ snapshot: operand.window === "chart" ? "快照起点" : "预警窗口起点", session: "UTC 日界" }}
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
  const options = schema.operands.filter((source) => {
    if (context.timing === "bar_close") return source !== "event";
    if (operand.source === "event") return source === "event";
    return source !== "event" && source !== "bar" && source !== "derivative";
  });
  return (
    <div className="rule-operand">
      <Choice label={label} value={operand.source} options={options} labels={operandLabels}
        onChange={(source) => onChange(newOperand(source, context.defaults))} />
      {operand.source === "constant" && (
        <DecimalField label="数值" value={operand.value} invalid={!validDecimal(operand.value)}
          onChange={(value) => onChange({ ...operand, value })} />
      )}
      {operand.source === "bar" && (
        <Choice label="字段" value={operand.field} options={schema.barFields} labels={barLabels}
          onChange={(field) => onChange({ ...operand, field })} />
      )}
      {operand.source === "event" && (
        <Choice label="行情字段" value={operand.metric} options={schema.eventMetrics} labels={METRIC_LABELS}
          onChange={(metric) => onChange({ ...operand, metric })} />
      )}
      {operand.source === "derivative" && (
        <Choice label="衍生品字段" value={operand.kind} options={schema.derivatives} labels={METRIC_LABELS}
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
        <Choice label="边界" value={operand.edge ?? "upper"} options={schema.edges} labels={edgeLabels}
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
      <legend className="rule-sr-only">{expressionLabels[node.op]}</legend>
      <div className="rule-node-header">
        <Choice label="条件类型" value={node.op} options={types} labels={expressionLabels} onChange={changeType} />
        {onRemove && <button type="button" className="drawing-action" onClick={onRemove}>移除</button>}
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
              onClick={() => onChange({ ...node, args: [...node.args, newComparison(context.timing)] })}>添加条件</button>
          )}
        </div>
      )}
      {node.op === "compare" && (
        <div className="rule-comparison">
          <OperandFields label="左侧" operand={node.left} context={context} onChange={(left) => onChange({ ...node, left })} />
          <Choice label="比较" value={node.test}
            options={schema.comparisons.filter((test) => !intrabar || !test.startsWith("cross_"))}
            labels={comparisonLabels} onChange={(test) => onChange({ ...node, test })} />
          <OperandFields label="右侧" operand={node.right} context={context} onChange={(right) => onChange({ ...node, right })} />
        </div>
      )}
      {node.op === "overlay" && (
        <div className="rule-reference">
          <DrawingChoice id={node.drawing_id} drawings={context.drawings} ratio={node.ratio}
            onSelect={(drawing_id) => onChange({ ...node, drawing_id, ratio: null })}
            onRatio={(ratio) => onChange({ ...node, ratio })} />
          <Choice label="画线事件" value={node.mode}
            options={intrabar ? schema.overlayModes.filter((mode) => mode === "TOUCH") : schema.overlayModes}
            labels={{ TOUCH: "触及", CLOSE_BREAK: "收盘突破" }} onChange={(mode) => onChange({ ...node, mode })} />
          <Choice label="方向" value={node.direction} options={schema.directions}
            labels={{ ABOVE: "向上", BELOW: "向下" }} onChange={(direction) => onChange({ ...node, direction })} />
        </div>
      )}
      {node.op === "level_event" && (
        <div className="rule-reference">
          <LevelChoice id={node.level_id} context={context}
            onSelect={(level) => onChange({ ...node, level_id: level.level_id, level_kind: level.kind })} />
          <Choice label="关键位事件" value={node.event}
            options={intrabar ? schema.levelEvents.filter((event) => event === "TOUCH") : schema.levelEvents}
            labels={ALERT_LEVEL_EVENT_LABELS} onChange={(event) => onChange({ ...node, event })} />
        </div>
      )}
    </fieldset>
  );
}

export function RuleEditorForm({ expression, onChange, ...context }: FormContext & {
  expression: Expression | null;
  onChange: (expression: Expression | null) => void;
}) {
  if (expression === null) {
    return (
      <div className="rule-empty">
        <p>添加条件后，图表会自动预览规则。</p>
        <button type="button" className="drawing-action" onClick={() => onChange(newComparison(context.timing))}>
          添加条件
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
  return (
    <Choice label="求值时机" value={timing} options={schema.timings}
      labels={{ bar_close: "K 线收盘", event: "盘中行情事件" }} onChange={onChange} />
  );
}
