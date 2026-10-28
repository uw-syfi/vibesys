/**
 * What the reader sets on the Timeline, which the page keeps (`RunPageState.timeline`) so the
 * Timeline is the same at the run level and beside an idea it opened. Free of the rows it
 * selects (`timeline-rows.ts`), so the page state does not depend on them.
 */

/** How much of the run the Timeline shows. */
export type TimelineRange = 'hour' | 'day' | 'all';

/** A part of the run clock, in seconds. */
export interface ClockSpan {
  readonly from: number;
  readonly to: number;
}

/**
 * What the reader set on the Timeline, kept by the page so the Timeline is the same at the run
 * level and beside an idea it opened: the range, whether the failed lane is unfolded, and the
 * part of the range zoomed or panned to (null: all of it).
 */
export interface TimelineControls {
  readonly range: TimelineRange;
  readonly expanded: boolean;
  readonly looked: ClockSpan | null;
}

export const TIMELINE_CONTROLS_DEFAULT: TimelineControls = {
  range: 'all',
  expanded: false,
  looked: null,
};
