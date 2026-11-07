/**
 * The Cost feature's export: the screen of one run's cost. The run's own vocabulary comes in as
 * `CostWords`; nothing here imports another feature. Another screen that shows a run's cost in
 * brief (the run report) takes `SpendCard` from `spend-card.ts` with `costScreenModel(...).spend`.
 */
export {CostScreen} from './cost-screen.js';
export type {CostWords} from './words.js';
