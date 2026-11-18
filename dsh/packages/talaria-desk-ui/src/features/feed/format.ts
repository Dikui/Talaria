/** DecimalString → 定点小数字符串，仅用于展示，不参与计算。 */
export function formatPrice(value: string, precision: number): string {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) {
    return value;
  }
  return numeric.toFixed(Math.max(precision, 0));
}

/** 数量按精度裁剪，保留末尾 0 以对齐盘口列宽。 */
export function formatSize(value: string, precision: number): string {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) {
    return value;
  }
  return numeric.toFixed(Math.max(precision, 0));
}
