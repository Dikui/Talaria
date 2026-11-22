import { useCallback, useEffect, useReducer } from "react";
import type { Chart } from "klinecharts";
import { api } from "../../lib/api";
import { frameSignature, readChartFrame } from "../drawings/projection";
import { usePaperQuery } from "./usePaperQuery";

export function ExecutionBarrierLayer({ chart, instrument }: { chart: Chart | null; instrument: string }) {
  const query = useCallback(() => api.paperBarriers(instrument), [instrument]);
  const { value: positions } = usePaperQuery(query);
  const [, update] = useReducer((version: number) => version + 1, 0);
  const visible = chart !== null && Boolean(positions?.length);

  useEffect(() => {
    if (!visible || chart === null) return;
    let request = 0;
    let previous = "";
    function check() {
      if (chart === null) return;
      const next = frameSignature(chart);
      if (next !== previous) {
        previous = next;
        update();
      }
      request = requestAnimationFrame(check);
    }
    request = requestAnimationFrame(check);
    return () => cancelAnimationFrame(request);
  }, [chart, visible]);

  if (!visible || chart === null || !positions) return null;
  const frame = readChartFrame(chart);
  if (!frame) return null;
  return <div className="paper-barriers" data-group-id="EXECUTION_BARRIER" aria-label="模拟盘保护屏障">
    {positions.flatMap((position) => [
      { key: "stop", price: position.stop, label: "止损" },
      ...(position.take_profit ?? []).map((price, index) => ({ key: `target-${index}`, price, label: "止盈" })),
    ].map(({ key, price, label }) => {
      const pixel = chart.convertToPixel({ value: Number(price) }, { absolute: true }) as { y?: number };
      const y = pixel?.y;
      if (y === undefined || !Number.isFinite(y) || y < frame.paneTop || y > frame.paneTop + frame.paneHeight) return null;
      return <div key={`${position.sub_account_id}-${position.round_id}-${key}`} className={`paper-barrier paper-barrier--${key === "stop" ? "stop" : "target"}`}
        style={{ top: y }}><span>{position.sub_account_id} · {label} {price}</span></div>;
    }))}
  </div>;
}
