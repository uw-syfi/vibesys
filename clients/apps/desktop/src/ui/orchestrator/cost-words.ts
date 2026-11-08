import type {CostWords} from '../cost/index.js';
import type {CostOverviewWords} from '../cost-overview/index.js';
import type {DisplayClock} from '../display-time.js';
import {faultWord, harnessWord, roleWord, runWord} from './words.js';

/** The run's vocabulary, handed to the Cost screen it shows; its moments are on `clock`. */
export const costWords = (clock: DisplayClock): CostWords => ({
  role: roleWord,
  harness: harnessWord,
  fault: faultWord,
  moment: clock.datedMoment24,
});

/** The words of the aggregate Cost screen: the run's, with every moment told on `clock`. */
export const costOverviewWords = (clock: DisplayClock): CostOverviewWords => ({
  ...costWords(clock),
  run: runWord,
});
