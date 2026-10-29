/**
 * What an idea's comparison with its base is called on a screen. The framework compares each idea
 * with the code it built on, which is another idea's (`round 2`) or, when the run could not measure
 * its input, the only base there is; a ratio against it is never worded as one against the input.
 *
 * Pure over the candidates; memoized on the array.
 */
import type {BaseComparison, Candidate} from '@vibesys/run-state/candidates';
import {ratio} from './span-reading.js';

/** What a base nobody recognizes is called: the comparison names a commit no idea of the run kept. */
const UNKNOWN_BASE = 'its base';

const INDEX = new WeakMap<readonly Candidate[], ReadonlyMap<string, Candidate>>();

function byRevision(candidates: readonly Candidate[]): ReadonlyMap<string, Candidate> {
  const known = INDEX.get(candidates);
  if (known !== undefined) return known;
  const found = new Map<string, Candidate>();
  for (const item of candidates) {
    for (const attempt of item.attempts) {
      if (attempt.revision !== null) found.set(attempt.revision, item);
    }
  }
  INDEX.set(candidates, found);
  return found;
}

/** The base `against` names in words: its idea's round (`round 2`), else its title, else `its base`. */
export function baseWords(candidates: readonly Candidate[], against: BaseComparison): string {
  const base = byRevision(candidates).get(against.baseCommit);
  if (base === undefined) return UNKNOWN_BASE;
  return base.round === null ? base.title : `round ${base.round}`;
}

/**
 * A comparison as a cell says it: the ratio, then its base. `whole` reads `1.10× vs round 2`; `short`
 * fits a narrow column (`1.10× r2`, `1.22× base` when the base is no idea of the run) and the
 * screen shows the whole on hover.
 */
export function againstWords(
  candidates: readonly Candidate[],
  against: BaseComparison,
  form: 'whole' | 'short',
): string {
  const found = ratio(against.ratio);
  if (form === 'whole') return `${found} vs ${baseWords(candidates, against)}`;
  const base = byRevision(candidates).get(against.baseCommit);
  return `${found} ${base === undefined || base.round === null ? 'base' : `r${base.round}`}`;
}
