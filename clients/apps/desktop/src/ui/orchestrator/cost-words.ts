import type {CostWords} from '../cost/index.js';
import {faultWord, formatMoment, harnessWord, roleWord} from './words.js';

/** The run's vocabulary, handed to the Cost screen it shows. */
export const COST_WORDS: CostWords = {
  role: roleWord,
  harness: harnessWord,
  fault: faultWord,
  moment: formatMoment,
};
