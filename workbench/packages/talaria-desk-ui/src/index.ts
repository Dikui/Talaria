export {
  VELA_ATTRIBUTION,
  roundsToBarriers,
  type ChartMountOptions,
  type ChartStats,
  type ChartSurfaceAdapter,
  type DrawingSyncSource,
  type ExecutionBarrierItem,
  type RenderBackendMode,
} from "./chart/adapter";
export { VelaChartAdapter } from "./chart/VelaChartAdapter";
export { KLineChartAdapter, type KLineChartRuntime } from "./chart/KLineChartAdapter";
export { VelaMarketFeed, type VelaMarketFeedOptions } from "./chart/VelaMarketFeed";
export {
  MAX_WEBGL_CELLS,
  WebglBudgetManager,
  useWebglBudget,
  webglBudgetManager,
  type WebglBudgetSnapshot,
} from "./chart/webglBudget";
export {
  LAYER_AGENT_ANNOTATION,
  LAYER_EXECUTION_BARRIER,
  LAYER_SYSTEM_LEVEL,
  hitTestAgentAnnotation,
  hitTestSystemLevel,
} from "./chart/velaLayers";
export {
  VelaDrawingSyncBridge,
  contractDrawingToSerialized,
  serializedDrawingToCreate,
} from "./chart/drawingBridge";
