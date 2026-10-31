/**
 * An idea's status in the Ideas list, from typed facts only: its outcome, the reason its span gives
 * (a closed set of identifiers), the verdict of its measurement and what it measured against the
 * input. No prose is read: a status never depends on how any text is worded.
 *
 * The statuses partition the ideas: each idea has exactly one, so the chips' counts sum to the
 * list's total.
 */
import type {Candidate} from '@vibesys/run-state/candidates';
import type {Change} from './headline.js';
import {TURNED_DOWN, words} from './span-reading.js';

/** The statuses, in the order the chips show them. */
export const IDEA_STATUSES = [
  'running',
  'faster',
  'no-gain',
  'not-measured',
  'dropped',
  'failed',
] as const;

export type IdeaStatus = (typeof IDEA_STATUSES)[number];

/** A status in a word. */
export const STATUS_WORDS: Readonly<Record<IdeaStatus, string>> = {
  running: 'Running',
  faster: 'Faster',
  'no-gain': 'No gain',
  'not-measured': 'Not measured',
  dropped: 'Dropped',
  failed: 'Failed',
};

/** An idea's status, and under it why when its span says. */
export interface IdeaStatusView {
  readonly status: IdeaStatus;
  /** `Turned down`, `Not implemented`, `Review rejected`: the cause under the word; null for none. */
  readonly cause: string | null;
}

/** The reason of an idea that never reached a verdict about its result: it was given up, not judged. */
const DROPPED: ReadonlySet<string> = new Set([
  TURNED_DOWN,
  'no_design',
  'not_opened',
  'stopped',
  'ended',
  'out-of-budget',
]);

/** The words of the reasons that read badly as their identifier does (`implementation-failed`). */
const CAUSE_WORDS: Readonly<Record<string, string>> = {
  [TURNED_DOWN]: 'Turned down',
  'implementation-failed': 'Not implemented',
  'review-rejected': 'Review rejected',
};

/** Whether `reason` has the shape of an identifier (`no_design`, `turn-limit`), not a sentence. */
function isIdentifier(reason: string): boolean {
  return /^[a-z][a-z0-9]*(?:[_-][a-z0-9]+)*$/.test(reason);
}

/** The cause a reason states under its status; null for a reason that is no identifier. */
function causeOf(reason: string | null, status: IdeaStatus): string | null {
  if (reason === null || !isIdentifier(reason)) return null;
  const named = CAUSE_WORDS[reason] ?? words(reason);
  const cause = `${named.charAt(0).toUpperCase()}${named.slice(1)}`;
  // A cause that only repeats the status says nothing (`Failed`, `Dropped`).
  return cause.toLowerCase() === STATUS_WORDS[status].toLowerCase() ? null : cause;
}

/**
 * `idea`'s status. `change` is what its readings made of the input (or, with none, of what the run
 * compares with) over every objective: `faster` when any objective improved, `same` or `slower`
 * when it measured and none did, null when nothing says; `measured` is whether it has a reading.
 *
 * A passed idea is `Faster` or `No gain` by what it measured (`No gain` when nothing to compare
 * with says it gained), `Not measured` without a reading.
 * A failed one: the reason `not_measured` is `Not measured`; a failed check or a verdict
 * `failed_check` is `Failed` (`Check failed`); one that was measured (or whose verdict is `slower`)
 * is `No gain`; with no reading, a reason that gave the idea up (`turned_down`, `no_design`, ...) is
 * `Dropped` and any other reason is `Failed`.
 */
export function ideaStatus(
  idea: Candidate,
  change: Change | null,
  measured: boolean,
): IdeaStatusView {
  const none = (status: IdeaStatus): IdeaStatusView => ({status, cause: null});
  if (idea.outcome === 'running') return none('running');
  if (idea.outcome === 'passed') {
    if (change === null && !measured) return none('not-measured');
    return none(change === 'faster' ? 'faster' : 'no-gain');
  }
  const measurement = idea.measurement;
  if (idea.reason === 'not_measured') return none('not-measured');
  if (measurement?.failed != null || measurement?.verdict === 'failed_check') {
    return {status: 'failed', cause: 'Check failed'};
  }
  // It was measured and turned down, or came out slower: a result, with no gain in it.
  if (measurement?.verdict === 'slower' || change !== null || measured) return none('no-gain');
  const status: IdeaStatus =
    idea.reason !== null && DROPPED.has(idea.reason) ? 'dropped' : 'failed';
  return {status, cause: causeOf(idea.reason, status)};
}
