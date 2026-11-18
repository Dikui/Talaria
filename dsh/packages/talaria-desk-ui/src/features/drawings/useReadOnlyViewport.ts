import { useEffect, useState } from "react";

import { isReadOnlyViewport, READ_ONLY_MAX_WIDTH } from "./responsive";

/** 390 窄屏（<=400px）只读：隐藏工具栏、禁用编辑控件，但保留画线展示。 */
export function useReadOnlyViewport(): boolean {
  const [readOnly, setReadOnly] = useState(() => {
    if (typeof window === "undefined") return false;
    return isReadOnlyViewport(window.innerWidth);
  });

  useEffect(() => {
    if (typeof window === "undefined") return;
    const query = window.matchMedia(`(max-width: ${READ_ONLY_MAX_WIDTH}px)`);
    const update = () => setReadOnly(isReadOnlyViewport(window.innerWidth));
    update();
    query.addEventListener("change", update);
    return () => query.removeEventListener("change", update);
  }, []);

  return readOnly;
}
