import { useCallback, useEffect, useState } from "react";
import type {
  AlertCreate,
  AlertPatch,
  AlertRecord,
  AlertTriggerRecord,
} from "@talaria/contracts";

import { api } from "../../lib/api";

export interface AlertsController {
  alerts: AlertRecord[];
  triggers: AlertTriggerRecord[];
  toast: AlertTriggerRecord | null;
  unread: number;
  loading: boolean;
  error: string | null;
  soundEnabled: boolean;
  setSoundEnabled: (enabled: boolean) => void;
  refresh: () => Promise<void>;
  create: (payload: AlertCreate) => Promise<AlertRecord | null>;
  update: (alertId: string, patch: AlertPatch) => Promise<AlertRecord | null>;
  remove: (alertId: string) => Promise<void>;
  confirm: (alertId: string) => Promise<void>;
  dismiss: (alertId: string) => Promise<void>;
  ingestTrigger: (trigger: AlertTriggerRecord) => void;
  markSeen: () => void;
  clearToast: () => void;
}

export function useAlerts(): AlertsController {
  const [alerts, setAlerts] = useState<AlertRecord[]>([]);
  const [triggers, setTriggers] = useState<AlertTriggerRecord[]>([]);
  const [toast, setToast] = useState<AlertTriggerRecord | null>(null);
  const [unread, setUnread] = useState(0);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [soundEnabled, setSoundEnabled] = useState(false);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const [alertRes, triggerRes] = await Promise.all([
        api.alerts(),
        api.alertTriggers({ limit: 30 }),
      ]);
      setAlerts(Array.isArray(alertRes.alerts) ? alertRes.alerts : []);
      setTriggers(Array.isArray(triggerRes.triggers) ? triggerRes.triggers : []);
      setError(null);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Failed to load alerts");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const create = useCallback(async (payload: AlertCreate) => {
    try {
      const record = await api.createAlert(payload);
      setAlerts((prev) => [record, ...prev]);
      setError(null);
      return record;
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Failed to create alert");
      return null;
    }
  }, []);

  const update = useCallback(async (alertId: string, patch: AlertPatch) => {
    try {
      const record = await api.patchAlert(alertId, patch);
      setAlerts((prev) => prev.map((a) => (a.alert_id === alertId ? record : a)));
      setError(null);
      return record;
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Failed to update alert");
      return null;
    }
  }, []);

  const remove = useCallback(async (alertId: string) => {
    try {
      await api.deleteAlert(alertId);
      setAlerts((prev) => prev.filter((a) => a.alert_id !== alertId));
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Failed to delete alert");
    }
  }, []);

  const confirm = useCallback(async (alertId: string) => {
    try {
      const updated = await api.confirmAlert(alertId);
      setAlerts((prev) => prev.map((a) => (a.alert_id === alertId ? updated : a)));
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Failed to confirm alert");
    }
  }, []);

  const dismiss = useCallback(async (alertId: string) => {
    try {
      const updated = await api.dismissAlert(alertId);
      setAlerts((prev) => prev.map((a) => (a.alert_id === alertId ? updated : a)));
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Failed to dismiss alert");
    }
  }, []);

  const ingestTrigger = useCallback((trigger: AlertTriggerRecord) => {
    setTriggers((prev) =>
      prev.some((t) => t.trigger_id === trigger.trigger_id)
        ? prev
        : [trigger, ...prev].slice(0, 50),
    );
    setToast(trigger);
    setUnread((n) => n + 1);
  }, []);

  const markSeen = useCallback(() => setUnread(0), []);
  const clearToast = useCallback(() => setToast(null), []);

  return {
    alerts,
    triggers,
    toast,
    unread,
    loading,
    error,
    soundEnabled,
    setSoundEnabled,
    refresh,
    create,
    update,
    remove,
    confirm,
    dismiss,
    ingestTrigger,
    markSeen,
    clearToast,
  };
}
