/**
 * The Cost feature's export: the screen of one run's cost, and the parts any screen of cost shares
 * (the chart, the splits by agent type and by model, and the way figures are written). The run's own
 * vocabulary comes in as `CostWords`; nothing here imports another feature. The run report writes
 * a run's cost in brief with the same figures (`usdOrNothing`, `formatTokens`, `formatCount`).
 */

export {AgentTypeTable, agentTypeLine} from './agent-types.js';
export {FLOOR_HINT, floored} from './cost-cell.js';
export {
  type SpendChart,
  slotsOf,
  spendChart,
  type TokenSplit,
  tokenSplit,
} from './cost-model.js';
export {CostScreen} from './cost-screen.js';
export {
  bucketWords,
  type CostWords,
  FLOOR,
  formatAxisUsd,
  formatCount,
  formatUsd,
  isModelId,
  modelKey,
  modelName,
  NOT_REPORTED,
  type Precision,
  precisionOf,
  usdOrNothing,
} from './words.js';
