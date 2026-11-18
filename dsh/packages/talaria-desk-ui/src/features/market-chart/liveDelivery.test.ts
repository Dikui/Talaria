import { describe, expect, it } from "vitest";

import { kline, MINUTE, BASE } from "../feed/testData";

import { shouldDeliverLiveBar } from "./liveDelivery";

describe("shouldDeliverLiveBar", () => {
  it("与快照最后一根相同的 timestamp 继续投递", () => {
    const event = kline(BASE, { revision: 1 });
    expect(shouldDeliverLiveBar(event, BASE, 0)).toBe(true);
  });

  it("比快照最后一根更旧的 timestamp 不投递", () => {
    const event = kline(BASE - MINUTE, { revision: 3 });
    expect(shouldDeliverLiveBar(event, BASE, undefined)).toBe(false);
  });

  it("同一 timestamp 的低或相同 revision 不重复投递", () => {
    const event = kline(BASE, { revision: 2 });
    expect(shouldDeliverLiveBar(event, BASE, 2)).toBe(false);
    expect(shouldDeliverLiveBar(event, BASE, 3)).toBe(false);
  });

  it("同一 timestamp 的高 revision 投递", () => {
    const event = kline(BASE, { revision: 4 });
    expect(shouldDeliverLiveBar(event, BASE, 3)).toBe(true);
  });
});
