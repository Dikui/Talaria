export const READ_ONLY_MAX_WIDTH = 400;
export const MOBILE_DRAWER_MAX_WIDTH = 768;

export function isReadOnlyViewport(width: number): boolean {
  return Number.isFinite(width) && width <= READ_ONLY_MAX_WIDTH;
}

export function isMobileDrawerViewport(width: number): boolean {
  return Number.isFinite(width) && width <= MOBILE_DRAWER_MAX_WIDTH;
}
