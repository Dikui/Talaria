export function formatPrice(value: string, precision: number): string {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) {
    return value;
  }
  return numeric.toFixed(Math.max(precision, 0));
}

export function formatSize(value: string, precision: number): string {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) {
    return value;
  }
  return numeric.toFixed(Math.max(precision, 0));
}
