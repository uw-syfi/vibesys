/**
 * The folded run at the view's sequence: what the Overview and Work tabs read. The screen's live
 * connection folds the run's events up to the view (`foldTrace`, `foldOverview` from
 * `@vibesys/run-state`) and hands the result here, so a past view and the live one take the same
 * selectors.
 */
import type {RunOverview, Span} from '@vibesys/contracts';
import {EMPTY_TRACE, type RunTrace, traceSpans} from '@vibesys/run-state/trace';

export interface RunAtView {
  readonly trace: RunTrace;
  /** The run's declared overview, or null before `run_started` declared one. */
  readonly overview: RunOverview | null;
}

/** A run with nothing folded yet: no spans and no overview. */
export const NOTHING_FOLDED: RunAtView = {trace: EMPTY_TRACE, overview: null};

export function spansAtView(run: RunAtView): readonly Span[] {
  return traceSpans(run.trace);
}
