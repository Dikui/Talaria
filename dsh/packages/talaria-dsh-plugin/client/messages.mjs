/**
 * Every piece of visible plugin copy. The same entries feed React through
 * desk-ui's locale store and host-rendered labels through ctx.locale.
 */
export const MESSAGES = {
  offline: {
    zh: 'Talaria 数据引擎（talaria-data）未运行。请打开独立看盘工作台或运行 talaria-data up。',
    en: 'Talaria data engine (talaria-data) is offline. Open Talaria Desk or run talaria-data up.',
  },
  connecting: {
    zh: '正在连接 Talaria 数据引擎…',
    en: 'Connecting to talaria-data…',
  },
  retry: { zh: '重试', en: 'Retry' },
  workspace: { zh: 'Talaria 工作台', en: 'Talaria Workspace' },
  workspaceTabs: { zh: 'Talaria 工作台标签', en: 'Talaria workspace tabs' },
  chartTab: { zh: 'Talaria 看盘', en: 'Talaria Chart' },
  utilityDock: { zh: '功能右栏', en: 'Utility dock' },
  dockAria: { zh: 'Talaria 功能右栏', en: 'Talaria utility dock' },
  closeDock: { zh: '关闭功能右栏', en: 'Close utility dock' },
  dockToChart: { zh: '停靠图表右栏', en: 'Dock beside chart' },
  openMain: { zh: '在主区打开', en: 'Open in main area' },
  openWindow: { zh: '独立窗口', en: 'Standalone window' },
  guideDescription: {
    zh: '在右侧面板打开确定性历史行情与指标工作台',
    en: 'Open the deterministic history and indicator workbench in the right panel',
  },
  linkTitle: { zh: 'Talaria 面板联动', en: 'Talaria panel link' },
}
