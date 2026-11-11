/**
 * The Cost feature's export: the screen of one run's cost, and the parts any screen of cost shares
 * (the chart, the splits by agent type and by model, and the way figures are written). The run's own
 * vocabulary comes in as `CostWords`; nothing here imports another feature. The run report writes
 * a run's cost in brief with the same figures (`usdOrNothing`, `formatTokens`, `formatCount`).
 */

export {AgentTypeTable, agentTypeLine} from './agent-types.js';
export {FLOOR_HINT, floored} from './cost-cell.js';
export {
  type BarMarker,
  type CostSplits,
  costSplits,
  MODEL_COLUMNS,
  type SpendChart,
  slotsOf,
  spendChart,
  type TokenSplit,
  TYPE_COLUMNS,
  tokenSplit,
} from './cost-model.js';
export {Counted, FactDot, MutedFact} from './fact-parts.js';
export {CostScreen, SpendOverTime} from './cost-screen.js';
export {SpendCard} from './spend-card.js';
export {
  type CostWords,
  FLOOR,
  formatAxisUsd,
  formatCount,
  formatRate,
  formatUsd,
  modelDisplayName,
  modelKey,
  modelName,
  NOT_REPORTED,
  type Precision,
  precisionOf,
  usdOrNothing,
} from './words.js';
