import { describe, expect, it } from "vitest";

import { isReadOnlyViewport, READ_ONLY_MAX_WIDTH } from "./responsive";

describe("窄屏只读判定", () => {
  it("390×844 与 400px 断点为只读", () => {
    expect(isReadOnlyViewport(390)).toBe(true);
    expect(isReadOnlyViewport(READ_ONLY_MAX_WIDTH)).toBe(true);
  });

  it("401px 与 1440×900 可编辑", () => {
    expect(isReadOnlyViewport(401)).toBe(false);
    expect(isReadOnlyViewport(1440)).toBe(false);
  });

  it("未知宽度不复用只读状态", () => {
    expect(isReadOnlyViewport(Number.NaN)).toBe(false);
  });
});
