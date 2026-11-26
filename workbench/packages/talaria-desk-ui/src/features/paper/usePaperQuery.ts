import { useEffect, useState } from "react";

import { t } from "../../lib/i18n";

export function usePaperQuery<T>(query: () => Promise<T>, interval = 5000) {
  const [result, setResult] = useState<{ query: typeof query; value?: T; error?: string }>();
  const [revision, setRevision] = useState(0);

  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    async function load() {
      try {
        const value = await query();
        if (!cancelled) setResult({ query, value });
      } catch (error) {
        if (!cancelled) {
          setResult({
            query,
            error:
              error instanceof Error
                ? error.message
                : t("读取模拟盘失败", "Failed to load paper trading"),
          });
        }
      } finally {
        if (!cancelled && interval > 0) timer = setTimeout(load, interval);
      }
    }
    void load();
    return () => {
      cancelled = true;
      if (timer !== undefined) clearTimeout(timer);
    };
  }, [query, interval, revision]);

  return {
    ...(result?.query === query ? result : undefined),
    refresh: () => setRevision((value) => value + 1),
  };
}
