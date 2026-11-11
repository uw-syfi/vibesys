/**
 * A cost as a cell of a table writes it, the same on every page: the figure (with `≥` while turns
 * without a price are behind it, and the count of them on hover), `unknown` where turns ran and none
 * was priced (unknown is never zero), and `—` only where no turn has run.
 */
import {plural, UNKNOWN} from '../plain-words.js';
import {FLOOR, formatCount, NOT_REPORTED, type Precision, usdOrNothing} from './words.js';

/** A cost as a cell writes it, and what hovering it says of the turns that have no price. */
export interface CostCell {
  readonly text: string;
  readonly hint: string | undefined;
}

/** What a `≥` on a figure says on hover: the same words on the run's facts line and on every cell. */
export const FLOOR_HINT =
  'At least this much: turns that reported tokens and no price are not in it.';

const unpricedHint = (turns: number): string =>
  `${formatCount(turns)} ${plural(turns, 'turn', 'turns')} reported tokens and no price. ` +
  'Their tokens are counted and their cost is not, so the cost is at least this.';

/** A figure with the mark of a floor before it; the mark and the figure are one word to the eye (a no-break space). */
export const floored = (figure: string): string => `${FLOOR.trim()} ${figure}`;

export function costCell(
  usage: {readonly costUsd: number | null},
  turns: number,
  unpricedTurns: number,
  precision: Precision,
): CostCell {
  const usd = usdOrNothing(usage.costUsd, precision);
  if (usd === null) {
    return turns === 0
      ? {text: NOT_REPORTED, hint: undefined}
      : {text: UNKNOWN, hint: 'No turn reported a price.'};
  }
  return unpricedTurns > 0
    ? {text: floored(usd), hint: unpricedHint(unpricedTurns)}
    : {text: usd, hint: undefined};
}
