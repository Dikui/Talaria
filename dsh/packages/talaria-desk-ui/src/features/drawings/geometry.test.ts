import { describe, expect, it } from "vitest";

import {
  buildDrawingGeometry,
  distanceToSegment,
  formatPrice,
  fitText,
  placeLabel,
  priceToNumber,
} from "./geometry";

const SIZE = { width: 600, height: 400 };

function anchor(x: number, y: number, price?: string) {
  return { x, y, price };
}

describe("formatPrice", () => {
  it("固定小数位输出 DecimalStr", () => {
    expect(formatPrice(101.1, 2)).toBe("101.10");
    expect(formatPrice(100, 0)).toBe("100");
    expect(formatPrice(0.123456, 4)).toBe("0.1235");
  });

  it("把 JSON 价格字符串解析为绘图用的 number", () => {
    expect(priceToNumber("101.10")).toBe(101.1);
    expect(priceToNumber("-0.5")).toBe(-0.5);
  });
});

describe("placeLabel", () => {
  it("右侧贴边时翻转为右对齐并留在画布内", () => {
    const placed = placeLabel(595, 120, SIZE, "这是一段比较长的标注文本");
    expect(placed.anchor).toBe("end");
    expect(placed.x).toBeLessThanOrEqual(SIZE.width - 2);
    expect(placed.y).toBeGreaterThanOrEqual(0);
  });

  it("底部贴边时翻转到锚点上方", () => {
    const placed = placeLabel(120, 392, SIZE, "底部标注");
    expect(placed.y + placed.height).toBeLessThanOrEqual(SIZE.height);
  });

  it("普通位置保持左对齐", () => {
    const placed = placeLabel(120, 120, SIZE, "标注");
    expect(placed.anchor).toBe("start");
    expect(placed.x).toBe(120);
    expect(placed.y).toBe(120);
  });
});

describe("buildDrawingGeometry", () => {
  it("水平线横贯画布并带价格标签", () => {
    const geometry = buildDrawingGeometry(
      "HORIZONTAL_LINE",
      [anchor(250, 100, "101.10")],
      { size: SIZE },
    );
    expect(geometry.lines).toEqual([
      { x1: 0, y1: 100, x2: SIZE.width, y2: 100, dashed: false },
    ]);
    expect(geometry.labels).toHaveLength(1);
    expect(geometry.labels[0].text).toBe("101.10");
    expect(geometry.anchors).toEqual([{ x: 250, y: 100 }]);
  });

  it("射线延伸到画布边界外的可视区域", () => {
    const geometry = buildDrawingGeometry("RAY", [anchor(100, 100), anchor(200, 50)], {
      size: SIZE,
    });
    const [line] = geometry.lines;
    expect(line.x1).toBe(100);
    expect(line.y1).toBe(100);
    // 右上方向：先撞到顶部或右边界，终点在画布边上。
    expect(line.x2 === SIZE.width || line.y2 === 0).toBe(true);
    expect(line.x2).toBeGreaterThanOrEqual(100);
    expect(line.y2).toBeLessThanOrEqual(100);
  });

  it("线段只连接两个锚点", () => {
    const geometry = buildDrawingGeometry("SEGMENT", [anchor(20, 30), anchor(220, 130)], {
      size: SIZE,
    });
    expect(geometry.lines).toEqual([
      { x1: 20, y1: 30, x2: 220, y2: 130, dashed: false },
    ]);
  });

  it("平行通道给出两条平行线与填充多边形", () => {
    const geometry = buildDrawingGeometry(
      "PARALLEL_CHANNEL",
      [anchor(100, 200), anchor(300, 150), anchor(300, 250)],
      { size: SIZE },
    );
    expect(geometry.lines).toHaveLength(2);
    const [first, second] = geometry.lines;
    expect(first.y1 - second.y1).toBeCloseTo(first.y2 - second.y2, 6);
    expect(geometry.polygons).toHaveLength(1);
    expect(geometry.polygons[0].points).toHaveLength(4);
  });

  it("斐波那契回撤给出 7 条等比水平线与标签", () => {
    const geometry = buildDrawingGeometry(
      "FIB_RETRACEMENT",
      [anchor(100, 100, "110.00"), anchor(300, 300, "100.00")],
      { size: SIZE },
    );
    expect(geometry.lines).toHaveLength(7);
    expect(geometry.labels).toHaveLength(7);
    expect(geometry.lines[0].y1).toBeCloseTo(300, 6);
    expect(geometry.lines[6].y1).toBeCloseTo(100, 6);
    expect(geometry.labels[0].text).toContain("0.0%");
    expect(geometry.labels[6].text).toContain("100.0%");
  });

  it("矩形支持反向拖动并归一化为正尺寸", () => {
    const geometry = buildDrawingGeometry("RECTANGLE", [anchor(300, 260), anchor(120, 140)], {
      size: SIZE,
    });
    expect(geometry.rects).toEqual([{ x: 120, y: 140, width: 180, height: 120 }]);
  });

  it("文本标注使用贴边翻转后的位置", () => {
    const geometry = buildDrawingGeometry("TEXT_LABEL", [anchor(570, 380)], {
      size: SIZE,
      text: "很长的贴边文本标注",
    });
    expect(geometry.labels[0].text).toBe("很长的贴边文本标注");
    expect(geometry.labels[0].anchor).toBe("end");
    expect(geometry.labels[0].y + 18).toBeLessThanOrEqual(SIZE.height);
  });

  it("超长文本在边缘被截断而不是被画布裁切", () => {
    const longText = "长文本".repeat(40);
    const geometry = buildDrawingGeometry("TEXT_LABEL", [anchor(590, 200)], {
      size: SIZE,
      text: longText,
    });
    const placed = geometry.labels[0];
    expect(placed.text.endsWith("…")).toBe(true);
    expect(placed.width).toBeLessThanOrEqual(SIZE.width);
    const left = placed.anchor === "end" ? placed.x - placed.width : placed.x;
    expect(left).toBeGreaterThanOrEqual(0);
    expect(left + placed.width).toBeLessThanOrEqual(SIZE.width);
    expect(fitText("标注", 600)).toBe("标注");
  });

  it("锚点不足时返回空几何，不抛错", () => {
    const geometry = buildDrawingGeometry("FIB_RETRACEMENT", [anchor(10, 10)], {
      size: SIZE,
    });
    expect(geometry.lines).toEqual([]);
  });
});

describe("distanceToSegment", () => {
  it("返回点到线段的最短距离", () => {
    expect(distanceToSegment(50, 20, 0, 0, 100, 0)).toBeCloseTo(20, 6);
    expect(distanceToSegment(-10, 0, 0, 0, 100, 0)).toBeCloseTo(10, 6);
    expect(distanceToSegment(130, 40, 0, 0, 100, 0)).toBeCloseTo(50, 6);
  });
});
