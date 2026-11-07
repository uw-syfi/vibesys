import type {CostWords} from '../cost/index.js';
import type {DisplayClock} from '../display-time.js';
import {faultWord, harnessWord, roleWord} from './words.js';

/** The run's vocabulary, handed to the Cost screen it shows; its moments are on `clock`. */
export const costWords = (clock: DisplayClock): CostWords => ({
  role: roleWord,
  harness: harnessWord,
  fault: faultWord,
  moment: clock.datedMoment24,
});
