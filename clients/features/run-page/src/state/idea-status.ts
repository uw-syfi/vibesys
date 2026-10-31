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
import {TURNED_DOWN} from './span-reading.js';

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

/**
 * The reasons a span gives for an idea that did not make it, and the cause each states under its
 * status. The set is closed: a reason this client does not know is a status alone, never its
 * identifier spelt out. A reason the backend adds is worded here in the same change
 * (`idea-status.test.ts` holds the fixtures' list of reasons to this table).
 */
export const REASON_WORDS = {
  turned_down: 'Turned down',
  not_measured: 'Not measured',
  no_design: 'No design',
  not_opened: 'Not opened',
  stopped: 'Stopped',
  'parent-unverified': 'Parent unverified',
  refused: 'Refused',
  ended: 'Ended',
  'out-of-budget': 'Out of budget',
  'turn-failed': 'Turn failed',
  'invalid-reply': 'Invalid reply',
  'render-failed': 'Render failed',
  'wait-refused': 'Wait refused',
  'checkpoint-declined': 'Checkpoint declined',
  'no-changed-candidate': 'No changed candidate',
  'implementation-failed': 'Not implemented',
  'review-rejected': 'Review rejected',
  'no-trusted-evidence': 'No trusted evidence',
  'evidence-refused': 'Evidence refused',
  'evidence-undecodable': 'Evidence undecodable',
  'profile-capture-unavailable': 'Profile capture unavailable',
  'turn-limit': 'Turn limit',
  'repeated-turn': 'Repeated turn',
  'merge-refused': 'Merge refused',
  'merge-unsound': 'Merge unsound',
  'merge-regressed': 'Merge regressed',
} as const;

type Reason = keyof typeof REASON_WORDS;

const isReason = (reason: string): reason is Reason => Object.hasOwn(REASON_WORDS, reason);

/** The reasons of an idea that never reached a verdict about its result: it was given up, not judged. */
const DROPPED: ReadonlySet<Reason> = new Set([
  TURNED_DOWN,
  'no_design',
  'not_opened',
  'stopped',
  'ended',
  'out-of-budget',
]);

/** The cause a known reason states under its status; null for a reason that is not one, and for one that only repeats the status. */
function causeOf(reason: string | null, status: IdeaStatus): string | null {
  if (reason === null || !isReason(reason)) return null;
  const cause = REASON_WORDS[reason];
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
    idea.reason !== null && isReason(idea.reason) && DROPPED.has(idea.reason)
      ? 'dropped'
      : 'failed';
  return {status, cause: causeOf(idea.reason, status)};
}
