/**
 * What a run's journal says at a glance, as the sidebar's rows read it: the helper's digest
 * (`runs.list`'s `digest`, feature `run_digest`) in the app's own terms, with when the run began.
 * It is how a run's ending is known once the registry's tombstone has expired (a day).
 */
import type {RunClosing, RunDigest, RunSummary} from '@vibesys/contracts/host-helper';
import type {RunEnd} from './listing.js';
import {type EndLook, endedLook} from './run-look.js';

/** One run's digest, or why it could not be read. */
export type RunDigestEntry =
  | {
      readonly kind: 'read';
      /** When the run was created, in milliseconds. */
      readonly createdAt: number;
      /** The ideas the run tried; null when its journal predates the run trace and records none. */
      readonly ideas: number | null;
      /** The best passed candidate's value as a multiple of the input's; null when none passed. */
      readonly bestRatio: number | null;
      readonly durationSeconds: number | null;
      /** The run's own ending record's outcome, when its journal has one that names it. */
      readonly outcome: NonNullable<RunEnd>['outcome'] | null;
      readonly closing: RunClosing | null;
      /** Whether the journal says the run is over (an ending record or a terminal event). */
      readonly over: boolean;
    }
  | {readonly kind: 'unreadable'; readonly createdAt: number; readonly message: string};

/** The answer to asking for digests: one per run the host knows among those asked, or no support. */
export type RunDigestsReply =
  | {readonly kind: 'digests'; readonly runs: ReadonlyMap<string, RunDigestEntry>}
  | {readonly kind: 'unsupported'};

function read(digest: RunDigest, createdAt: number): RunDigestEntry {
  const {ending, closing} = digest;
  return {
    kind: 'read',
    createdAt,
    ideas: digest.ideas,
    bestRatio: digest.best_ratio,
    durationSeconds: digest.duration_s,
    outcome: ending?.outcome ?? null,
    closing,
    over: ending !== null || closing !== null,
  };
}

/** The entries of the runs of `summaries` that carry a digest, by run id. */
export function digestEntries(summaries: readonly RunSummary[]): Map<string, RunDigestEntry> {
  const entries = new Map<string, RunDigestEntry>();
  for (const summary of summaries) {
    const {digest} = summary;
    if (digest === undefined || digest === null) continue;
    const createdAt = Date.parse(summary.created_at);
    entries.set(
      summary.run_id,
      digest.kind === 'digest'
        ? read(digest, createdAt)
        : {kind: 'unreadable', createdAt, message: digest.message},
    );
  }
  return entries;
}

/**
 * How a run the journal says is over ended: its ending record's outcome, else its terminal event
 * (failed, interrupted, finished); null when the journal does not say.
 */
export function digestLook(entry: Extract<RunDigestEntry, {kind: 'read'}>): EndLook | null {
  if (entry.outcome !== null) {
    return endedLook({
      phase: 'completed',
      end: {outcome: entry.outcome, reason: '', resumable: false},
    });
  }
  switch (entry.closing) {
    case 'finished':
      return endedLook({phase: 'completed', end: null});
    case 'failed':
      return endedLook({phase: 'failed', end: null});
    case 'interrupted':
      return endedLook({phase: 'stopped', end: null});
    default:
      return null;
  }
}

/** One run of a project as the host lists it: what an all-runs list needs before its digest is read. */
export interface ListedRun {
  readonly runId: string;
  /** The task it ran, or null when the run has no record of one (it stopped at start-up). */
  readonly task: string | null;
  /** When the run was created, in milliseconds. */
  readonly createdAt: number;
}

/** The runs of `summaries`, newest first (ties by run id, so the order is total). */
export function listedRuns(summaries: readonly RunSummary[]): ListedRun[] {
  return summaries
    .map(summary => ({
      runId: summary.run_id,
      task: summary.task ?? null,
      createdAt: Date.parse(summary.created_at),
    }))
    .sort((left, right) => right.createdAt - left.createdAt || (left.runId < right.runId ? 1 : -1));
}
