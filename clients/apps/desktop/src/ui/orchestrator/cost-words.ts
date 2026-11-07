import type {CostWords} from '../cost/index.js';
import type {CostOverviewWords} from '../cost-overview/index.js';
import {seattle} from './fleet-words.js';
import {faultWord, formatMoment, harnessWord, roleWord, runWord} from './words.js';

/** The run's vocabulary, handed to the Cost screen it shows. */
export const COST_WORDS: CostWords = {
  role: roleWord,
  harness: harnessWord,
  fault: faultWord,
  moment: formatMoment,
};

/** The words of the aggregate Cost screen: the run's, with every moment told in Seattle time. */
export const COST_OVERVIEW_WORDS: CostOverviewWords = {
  ...COST_WORDS,
  moment: seattle,
  run: runWord,
};
