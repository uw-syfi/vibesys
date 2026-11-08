/** The display zone for tests: pinned, so the text a screen writes is the same on every machine. */
import {makeDisplayClock} from './display-time.js';
import {costOverviewWords, costWords} from './orchestrator/cost-words.js';

export const TEST_ZONE = 'America/Los_Angeles';
export const TEST_CLOCK = makeDisplayClock(TEST_ZONE);

/** The Cost screen's words with its moments in the test zone. */
export const COST_WORDS = costWords(TEST_CLOCK);

/** The aggregate Cost screen's words with its moments in the test zone. */
export const COST_OVERVIEW_WORDS = costOverviewWords(TEST_CLOCK);
