/**
 * The windows of time the Usage page offers, and the span of milliseconds each one asks the
 * helper about. A window is rolling except Today, which begins at the local midnight the shell
 * names: this module reads no clock and no time zone.
 */
import {USAGE_WINDOW_MAX_HOURS} from '@vibesys/hosts';

export type UsageWindowChoice = 'today' | 'week' | 'fortnight';

export const USAGE_WINDOWS: readonly {
  readonly value: UsageWindowChoice;
  readonly label: string;
}[] = [
  {value: 'today', label: 'Today'},
  {value: 'week', label: 'This week'},
  {value: 'fortnight', label: 'Two weeks'},
];

/** What the shell reads of the clock when a question is asked, in milliseconds. */
export interface WindowClock {
  readonly now: number;
  /** The most recent local midnight at or before `now`. */
  readonly midnight: number;
}

/** The window `[since, until)` a question is asked over, in milliseconds. */
export interface UsageSpan {
  readonly since: number;
  readonly until: number;
}

const HOUR_MS = 3_600_000;

/** The span of `choice` ending now: it ends after it begins and never exceeds the helper's bound. */
export function spanOf(choice: UsageWindowChoice, clock: WindowClock): UsageSpan {
  const {now, midnight} = clock;
  switch (choice) {
    case 'today':
      return {since: Math.min(midnight, now - 1), until: now};
    case 'week':
      return {since: now - 7 * 24 * HOUR_MS, until: now};
    case 'fortnight':
      return {since: now - USAGE_WINDOW_MAX_HOURS * HOUR_MS, until: now};
  }
}
