import { describe, expect, it } from "vitest";
import type { CompactBar } from "@talaria/contracts";
import { VELA_ATTRIBUTION } from "./index";

describe("talaria-desk-ui scaffold", () => {
  it("exports Vela attribution metadata and contract types", () => {
    const sample: CompactBar = {
      timestamp: 1_700_000_000_000,
      open: "100.0",
      high: "101.0",
      low: "99.5",
      close: "100.5",
      volume: "10.0",
      turnover: "1005.0",
      status: "NORMAL",
      ts_available: 1_700_000_060_000,
      precision: "SECOND_VERIFIED",
    };
    expect(sample.status).toBe("NORMAL");
    expect(VELA_ATTRIBUTION.label).toBe("Vela");
    expect(VELA_ATTRIBUTION.url).toBe("https://velacharts.dev");
  });
});
