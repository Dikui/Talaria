import type { DrawingRecord } from "@talaria/contracts";
import { ALERT_OVERLAY_KINDS } from "../alerts/model";
import type { Expression, Timing } from "./ruleForm";

type Draft = { timing: Timing; expressions: Record<Timing, Expression | null> };

export class RuleDrafts {
  private saved = new Map<string, string>();
  private drafts = new Map<string, Draft>();
  rememberSaved(scope: string, fingerprint: string, alertId: string) {
    this.saved.set(JSON.stringify([scope, fingerprint]), alertId);
  }
  savedId(scope: string, fingerprint: string) {
    return this.saved.get(JSON.stringify([scope, fingerprint]));
  }
  read(scope: string): Draft {
    return this.drafts.get(scope) ?? { timing: "bar_close", expressions: { bar_close: null, event: null } };
  }
  write(scope: string, draft: Draft) {
    this.drafts.set(scope, draft);
  }
}

export function ruleDrawingOptions(drawings: readonly DrawingRecord[], instrument: string) {
  return drawings.filter(drawing => drawing.instrument_id === instrument && drawing.version > 0 &&
    drawing.deleted_at == null && ALERT_OVERLAY_KINDS.includes(drawing.kind));
}

export function savedAlertExists(id: string | undefined, alerts: readonly { alert_id: string }[]) {
  return id !== undefined && alerts.some(alert => alert.alert_id === id);
}
