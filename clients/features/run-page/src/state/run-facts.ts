/**
 * The run's core facts a model reads (`RunFacts`), from the run fold: its comparisons as they are,
 * and its quota stops placed on the run clock by the offset the page last heard. A page owns one
 * `createFactsReader` for its run: the same object comes back while neither moves, so the run
 * reader's caches hold. The offset drifts by the latency of each event, so a stop that moved less
 * than a second is the same pause and the pauses stay as they were.
 */
import type {CoreState} from '@vibesys/run-state';
import type {RunFold} from './live.js';
import {type QuotaPauseSpan, quotaPauses} from './quota-pauses.js';
import {NO_FACTS, type RunFacts} from './run-model.js';

/** An offset that moved less than this (seconds) does not move a pause. */
const OFFSET_TOLERANCE = 1;

function sameOffset(a: number | null, b: number | null): boolean {
  return a === b || (a !== null && b !== null && Math.abs(a - b) < OFFSET_TOLERANCE);
}

/** A reader of the facts of a run's fold; it remembers the last it built. */
export function createFactsReader(): (run: RunFold) => RunFacts {
  let pausesHeld: {
    stops: CoreState['quotaStops'];
    offset: number | null;
    pauses: readonly QuotaPauseSpan[];
  } | null = null;
  let facts: RunFacts = NO_FACTS;
  return run => {
    const core = run.core;
    if (core === null) return NO_FACTS;
    const offset = run.heard?.offset?.seconds ?? null;
    if (
      pausesHeld === null ||
      pausesHeld.stops !== core.quotaStops ||
      !sameOffset(pausesHeld.offset, offset)
    ) {
      pausesHeld = {stops: core.quotaStops, offset, pauses: quotaPauses(core.quotaStops, offset)};
    }
    if (facts.comparisons !== core.comparisons || facts.pauses !== pausesHeld.pauses) {
      facts = {comparisons: core.comparisons, pauses: pausesHeld.pauses};
    }
    return facts;
  };
}
