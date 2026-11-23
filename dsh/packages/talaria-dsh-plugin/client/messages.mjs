/**
 * Every piece of visible plugin copy. The same entries feed React through
 * desk-ui's locale store and host-rendered labels through ctx.locale.
 */
export const MESSAGES = {
  offline: {
    zh: 'Talaria 常驻服务离线。请打开 DeepSeek Harness 桌面应用或运行 talaria up --solo。',
    en: 'The Talaria service is offline. Open the DeepSeek Harness desktop app or run talaria up --solo.',
  },
  connecting: {
    zh: '正在连接 Talaria 常驻服务…',
    en: 'Connecting to the Talaria service…',
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
