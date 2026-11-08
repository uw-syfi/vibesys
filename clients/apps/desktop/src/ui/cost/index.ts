/**
 * The Cost feature's export: the screen of one run's cost, and the parts any screen of cost shares
 * (the chart, the splits by agent type and by model, and the way figures are written). The run's own
 * vocabulary comes in as `CostWords`; nothing here imports another feature. Another screen that
 * shows a run's cost in brief (the run report) takes `SpendCard` from `spend-card.ts` with
 * `costScreenModel(...).spend`.
 */
export {
  type CostSplits,
  costScreenModel,
  costSplits,
  MODEL_COLUMNS,
  TYPE_COLUMNS,
} from './cost-model.js';
export {CostScreen, SpendOverTime} from './cost-screen.js';
export {SpendCard} from './spend-card.js';
export {
  type CostWords,
  FLOOR,
  formatCount,
  formatRate,
  NOT_REPORTED,
  precisionOf,
  usdOrNothing,
} from './words.js';
