import { useCallback, useEffect, useState } from "react";
import type {
  AgentAnnotationRecord,
  AnnotationSessionSummary,
} from "@talaria/contracts";

import { api } from "../../lib/api";
import { errorMessage } from "../alerts/model";

const POLL_MS = 5_000;

export interface AnnotationsController {
  annotations: AgentAnnotationRecord[];
  sessions: AnnotationSessionSummary[];
  loading: boolean;
  error: string | null;
  refresh: () => Promise<void>;
  adopt: (annotationId: string) => Promise<AgentAnnotationRecord | null>;
  dismiss: (annotationId: string) => Promise<AgentAnnotationRecord | null>;
  clear: (sessionId?: string) => Promise<number | null>;
  clearError: () => void;
}

export function useAnnotations(options: {
  instrumentId: string;
  onDrawingsChanged: (instrumentId: string) => void;
}): AnnotationsController {
  const { instrumentId, onDrawingsChanged } = options;
  const [annotations, setAnnotations] = useState<AgentAnnotationRecord[]>([]);
  const [sessions, setSessions] = useState<AnnotationSessionSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const [list, sessionList] = await Promise.all([
        api.annotations({ instrumentId }),
        api.annotationSessions(),
      ]);
      setAnnotations(list.annotations);
      setSessions(sessionList.sessions);
      setError(null);
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      setLoading(false);
    }
  }, [instrumentId]);

  useEffect(() => {
    setLoading(true);
    void refresh();
    const timer = window.setInterval(() => {
      void refresh();
    }, POLL_MS);
    return () => window.clearInterval(timer);
  }, [refresh]);

  const adopt = useCallback(
    async (annotationId: string) => {
      try {
        const result = await api.adoptAnnotation(annotationId);
        if (result.annotation.adopted_drawing_id != null) onDrawingsChanged(result.annotation.instrument_id);
        await refresh();
        return result.annotation;
      } catch (caught) {
        setError(errorMessage(caught));
        return null;
      }
    },
    [onDrawingsChanged, refresh],
  );

  const dismiss = useCallback(
    async (annotationId: string) => {
      try {
        const record = await api.dismissAnnotation(annotationId);
        await refresh();
        return record;
      } catch (caught) {
        setError(errorMessage(caught));
        return null;
      }
    },
    [refresh],
  );

  const clear = useCallback(
    async (sessionId?: string) => {
      try {
        const result = await api.clearAnnotations(instrumentId, sessionId);
        await refresh();
        return result.cleared;
      } catch (caught) {
        setError(errorMessage(caught));
        return null;
      }
    },
    [instrumentId, refresh],
  );

  return {
    annotations,
    sessions,
    loading,
    error,
    refresh,
    adopt,
    dismiss,
    clear,
    clearError: () => setError(null),
  };
}
