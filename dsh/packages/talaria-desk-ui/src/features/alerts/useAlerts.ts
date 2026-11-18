import { useCallback, useEffect, useRef, useState } from "react";
import type {
  AlertCreate,
  AlertPatch,
  AlertRecord,
  AlertTriggerRecord,
} from "@talaria/contracts";

import { api } from "../../lib/api";
import { errorMessage } from "./model";

const POLL_MS = 5_000;
const TOAST_MS = 8_000;
const HISTORY_LIMIT = 50;
export const ALERT_SOUND_KEY = "talaria-alert-sound";

function readSoundPreference(): boolean {
  try {
    return window.localStorage.getItem(ALERT_SOUND_KEY) === "on";
  } catch {
    return false;
  }
}

/** 触发提示音默认关闭；只有用户显式打开才创建 AudioContext。 */
function playChime(): void {
  try {
    const Ctor =
      window.AudioContext ??
      (window as unknown as { webkitAudioContext?: typeof AudioContext })
        .webkitAudioContext;
    if (!Ctor) {
      return;
    }
    const context = new Ctor();
    const oscillator = context.createOscillator();
    const gain = context.createGain();
    oscillator.type = "sine";
    oscillator.frequency.setValueAtTime(880, context.currentTime);
    gain.gain.setValueAtTime(0.0001, context.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.08, context.currentTime + 0.02);
    gain.gain.exponentialRampToValueAtTime(0.0001, context.currentTime + 0.25);
    oscillator.connect(gain);
    gain.connect(context.destination);
    oscillator.start();
    oscillator.stop(context.currentTime + 0.26);
    oscillator.addEventListener("ended", () => {
      void context.close();
    });
  } catch {
    // 声音是可选增强，任何浏览器限制都静默忽略。
  }
}

export interface AlertsController {
  alerts: AlertRecord[];
  triggers: AlertTriggerRecord[];
  loading: boolean;
  error: string | null;
  toast: AlertTriggerRecord | null;
  unread: number;
  soundEnabled: boolean;
  refresh: () => Promise<void>;
  create: (payload: AlertCreate) => Promise<AlertRecord | null>;
  update: (alertId: string, patch: AlertPatch) => Promise<AlertRecord | null>;
  confirm: (alertId: string) => Promise<AlertRecord | null>;
  dismiss: (alertId: string) => Promise<AlertRecord | null>;
  remove: (alertId: string) => Promise<AlertRecord | null>;
  setEnabled: (alertId: string, enabled: boolean) => Promise<AlertRecord | null>;
  ingestTrigger: (trigger: AlertTriggerRecord) => void;
  clearToast: () => void;
  markSeen: () => void;
  setSoundEnabled: (enabled: boolean) => void;
  clearError: () => void;
}

export function useAlerts(): AlertsController {
  const [alerts, setAlerts] = useState<AlertRecord[]>([]);
  const [triggers, setTriggers] = useState<AlertTriggerRecord[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [toast, setToast] = useState<AlertTriggerRecord | null>(null);
  const [unread, setUnread] = useState(0);
  const [soundEnabled, setSoundState] = useState<boolean>(() =>
    typeof window === "undefined" ? false : readSoundPreference(),
  );
  const soundRef = useRef(soundEnabled);
  const seenRef = useRef<Set<string>>(new Set());
  const toastTimer = useRef<number | null>(null);

  useEffect(() => {
    soundRef.current = soundEnabled;
  }, [soundEnabled]);

  const refresh = useCallback(async () => {
    try {
      const [list, history] = await Promise.all([
        api.alerts(),
        api.alertTriggers({ limit: HISTORY_LIMIT }),
      ]);
      setAlerts(list.alerts);
      setTriggers(history.triggers);
      setError(null);
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
    const timer = window.setInterval(() => {
      void refresh();
    }, POLL_MS);
    return () => {
      window.clearInterval(timer);
      if (toastTimer.current !== null) {
        window.clearTimeout(toastTimer.current);
      }
    };
  }, [refresh]);

  const run = useCallback(
    async <T,>(action: () => Promise<T>): Promise<T | null> => {
      try {
        const result = await action();
        await refresh();
        setError(null);
        return result;
      } catch (caught) {
        setError(errorMessage(caught));
        return null;
      }
    },
    [refresh],
  );

  const create = useCallback(
    (payload: AlertCreate) => run(() => api.createAlert(payload)),
    [run],
  );
  const update = useCallback(
    (alertId: string, patch: AlertPatch) =>
      run(() => api.patchAlert(alertId, patch)),
    [run],
  );
  const confirm = useCallback(
    (alertId: string) => run(() => api.confirmAlert(alertId)),
    [run],
  );
  const dismiss = useCallback(
    (alertId: string) => run(() => api.dismissAlert(alertId)),
    [run],
  );
  const remove = useCallback(
    (alertId: string) => run(() => api.deleteAlert(alertId)),
    [run],
  );
  const setEnabled = useCallback(
    (alertId: string, enabled: boolean) =>
      run(() => api.patchAlert(alertId, { enabled })),
    [run],
  );

  const ingestTrigger = useCallback(
    (trigger: AlertTriggerRecord) => {
      if (seenRef.current.has(trigger.trigger_id)) {
        return;
      }
      seenRef.current.add(trigger.trigger_id);
      setTriggers((previous) =>
        [trigger, ...previous.filter((item) => item.trigger_id !== trigger.trigger_id)].slice(
          0,
          HISTORY_LIMIT,
        ),
      );
      setToast(trigger);
      setUnread((count) => count + 1);
      if (soundRef.current) {
        playChime();
      }
      if (toastTimer.current !== null) {
        window.clearTimeout(toastTimer.current);
      }
      toastTimer.current = window.setTimeout(() => {
        setToast(null);
        toastTimer.current = null;
      }, TOAST_MS);
      void refresh();
    },
    [refresh],
  );

  const clearToast = useCallback(() => {
    setToast(null);
    if (toastTimer.current !== null) {
      window.clearTimeout(toastTimer.current);
      toastTimer.current = null;
    }
  }, []);

  const markSeen = useCallback(() => setUnread(0), []);
  const clearError = useCallback(() => setError(null), []);

  const setSoundEnabled = useCallback((enabled: boolean) => {
    setSoundState(enabled);
    try {
      window.localStorage.setItem(ALERT_SOUND_KEY, enabled ? "on" : "off");
    } catch {
      // 隐私模式下写入失败不影响当前会话。
    }
  }, []);

  return {
    alerts,
    triggers,
    loading,
    error,
    toast,
    unread,
    soundEnabled,
    refresh,
    create,
    update,
    confirm,
    dismiss,
    remove,
    setEnabled,
    ingestTrigger,
    clearToast,
    markSeen,
    setSoundEnabled,
    clearError,
  };
}
