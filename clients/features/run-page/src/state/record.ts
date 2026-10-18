/**
 * The run's record in its host's latest listing: the newest live server driving it, else the newest
 * tombstone it left, else unlisted (the watch has not listed it, or its record expired).
 */
import type {HostListing, HostRun} from '@vibesys/hosts';
import {type RecentRun, type RunRef, recentStatus} from '@vibesys/runs';
import type {RunRecord} from './run-page.js';

function newest(runs: readonly HostRun[], runId: string, project?: string): HostRun | undefined {
  return runs
    .filter(run => run.runId === runId && (project === undefined || run.projectRoot === project))
    .reduce<HostRun | undefined>(
      (best, run) => (best === undefined || best.startedAt < run.startedAt ? run : best),
      undefined,
    );
}

export function runRecord(
  listings: readonly HostListing[],
  run: RunRef,
  recent?: RecentRun,
): RunRecord {
  const listing = listings.find(each => each.host === run.host);
  if (listing?.kind !== 'listed') return {kind: 'unlisted'};
  const live = newest(listing.live, run.runId, recent?.project);
  if (live !== undefined) return {kind: 'live', run: live};
  const ended = newest(listing.ended, run.runId, recent?.project);
  if (ended !== undefined) return {kind: 'ended', run: ended};
  if (recent === undefined) return {kind: 'unlisted'};
  const status = recentStatus(recent, listing);
  if (status.kind !== 'ended') return {kind: 'unlisted'};
  const phase = status.status;
  return {
    kind: 'historical',
    status: phase === 'stopped' || phase === 'completed' || phase === 'failed' ? phase : null,
  };
}

/** Whether two records say the same thing, so an unchanged listing is not folded again. */
export function sameRecord(left: RunRecord, right: RunRecord): boolean {
  if (left.kind === 'unlisted' || right.kind === 'unlisted') return left.kind === right.kind;
  if (left.kind === 'historical')
    return right.kind === 'historical' && left.status === right.status;
  if (right.kind === 'historical') return false;
  return left.kind === right.kind && left.run === right.run;
}
