import { describe, expect, it } from "vitest";

import { formatPrice, formatSize } from "./format";

describe("盘口数值格式化", () => {
  it("按精度补齐小数位", () => {
    expect(formatPrice("58000", 2)).toBe("58000.00");
    expect(formatPrice("58000.5", 1)).toBe("58000.5");
    expect(formatSize("0.5", 3)).toBe("0.500");
  });

  it("非数字原样返回", () => {
    expect(formatPrice("--", 2)).toBe("--");
  });
});
