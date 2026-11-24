import { useCallback, useEffect, useState } from "react";
import type { AgentAnnotationRecord } from "@talaria/contracts";

import { api } from "../../lib/api";

export interface AnnotationsController {
  annotations: AgentAnnotationRecord[];
  loading: boolean;
  error: string | null;
  refresh: () => Promise<void>;
  adopt: (annotationId: string) => Promise<void>;
  dismiss: (annotationId: string) => Promise<void>;
}

export function useAnnotations({
  instrumentId,
  onDrawingsChanged,
}: {
  instrumentId: string;
  onDrawingsChanged?: (instrumentId: string) => void;
}): AnnotationsController {
  const [annotations, setAnnotations] = useState<AgentAnnotationRecord[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const res = await api.annotations({ instrumentId });
      setAnnotations(Array.isArray(res.annotations) ? res.annotations : []);
      setError(null);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Failed to load annotations");
    } finally {
      setLoading(false);
    }
  }, [instrumentId]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const adopt = useCallback(
    async (annotationId: string) => {
      try {
        const res = await api.adoptAnnotation(annotationId);
        setAnnotations((prev) =>
          prev.map((item) =>
            item.annotation_id === annotationId ? res.annotation : item,
          ),
        );
        onDrawingsChanged?.(instrumentId);
      } catch (caught) {
        setError(caught instanceof Error ? caught.message : "Failed to adopt annotation");
      }
    },
    [instrumentId, onDrawingsChanged],
  );

  const dismiss = useCallback(async (annotationId: string) => {
    try {
      const updated = await api.dismissAnnotation(annotationId);
      setAnnotations((prev) =>
        prev.map((item) =>
          item.annotation_id === annotationId ? updated : item,
        ),
      );
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Failed to dismiss annotation");
    }
  }, []);

  return { annotations, loading, error, refresh, adopt, dismiss };
}
