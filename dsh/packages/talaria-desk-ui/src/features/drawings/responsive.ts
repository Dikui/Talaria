/**
 * 390 窄屏只读：任务单 05 要求窄屏仅展示已有画线，工具栏隐藏、编辑控件禁用。
 * 用 400px 作为与现有样式断点一致的阈值。
 */
export const READ_ONLY_MAX_WIDTH = 400;

export function isReadOnlyViewport(width: number): boolean {
  return Number.isFinite(width) && width <= READ_ONLY_MAX_WIDTH;
}
