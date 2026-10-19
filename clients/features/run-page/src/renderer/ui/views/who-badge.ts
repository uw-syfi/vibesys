import type {RowBadge} from '@vibesys/ui';
import {type Who, whoShort, whoWords} from '../../../state/index.js';

/**
 * The chip saying what ran a turn (`<model> on <harness>`; only the model in a narrow list), or
 * none when the run's records do not say. `more` is how many other harness and model pairs ran
 * the same row's other turns.
 */
export function whoBadge(who: Who | undefined, more = 0): RowBadge | null {
  if (who === undefined) return null;
  const words = whoWords(who);
  const short = whoShort(who);
  if (words === null || short === null) return null;
  const extra = more === 0 ? '' : ` +${more}`;
  return {label: `${words}${extra}`, short: `${short}${extra}`, tone: 'neutral', mono: true};
}
