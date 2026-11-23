import { useEffect, useState } from "react";

import { api, type CollaborationStatus } from "../../lib/api";
import { t, useLocale, type Locale } from "../../lib/i18n";

export interface CollaborationView {
  /** 尚未拿到第一次响应。 */
  loading: boolean;
  status: CollaborationStatus | null;
  label: string;
  tone: "neutral" | "online" | "warning";
}

const DAEMON_UNREACHABLE = "__daemon_unreachable__";

function viewOf(status: CollaborationStatus | null, locale: Locale): CollaborationView {
  if (status === null) {
    return { loading: true, status: null, label: t("协同宿主：检测中", "Collaboration host: checking"), tone: "neutral" };
  }
  if (status.mode === "solo" || status.host === "disabled") {
    return { loading: false, status, label: t("协同宿主：单机", "Collaboration host: solo"), tone: "neutral" };
  }
  if (status.host === "online") {
    return { loading: false, status, label: t("协同宿主：在线", "Collaboration host: online"), tone: "online" };
  }
  if (status.host === "starting") {
    return { loading: false, status, label: t("协同宿主：启动中", "Collaboration host: starting"), tone: "neutral" };
  }
  const message =
    status.message === DAEMON_UNREACHABLE
      ? t("Talaria 守护进程不可达", "Talaria daemon unreachable")
      : status.message;
  return {
    loading: false,
    status: { ...status, message },
    label: t("协同宿主：离线 · 快通道正常", "Collaboration host: offline · fast channel OK"),
    tone: "warning",
  };
}

/**
 * 轮询宿主状态。看盘数据不经过本请求：即使 DSH 停止或卡住，
 * 切图、指标与实时流仍由 Python 守护进程直接服务。
 */
export function useCollaborationStatus(intervalMs = 4000): CollaborationView {
  const locale = useLocale();
  const [status, setStatus] = useState<CollaborationStatus | null>(null);

  useEffect(() => {
    let cancelled = false;
    let timer = 0;
    const poll = () => {
      api
        .collaboration()
        .then((next) => {
          if (!cancelled) setStatus(next);
        })
        .catch(() => {
          if (!cancelled) {
            setStatus({
              mode: "dual",
              host: "offline",
              url: null,
              message: DAEMON_UNREACHABLE,
            });
          }
        });
    };
    poll();
    timer = window.setInterval(poll, intervalMs);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [intervalMs]);

  return viewOf(status, locale);
}
