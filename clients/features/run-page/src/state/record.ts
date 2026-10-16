/**
 * The run's record in its host's latest listing: the newest live server driving it, else the newest
 * tombstone it left, else unlisted (the watch has not listed it, or its record expired).
 */
import type {HostListing, HostRun} from '@vibesys/hosts';
import type {RunRef} from '@vibesys/runs';
import type {RunRecord} from './run-page.js';

function newest(runs: readonly HostRun[], runId: string): HostRun | undefined {
  return runs
    .filter(run => run.runId === runId)
    .reduce<HostRun | undefined>(
      (best, run) => (best === undefined || best.startedAt < run.startedAt ? run : best),
      undefined,
    );
}

export function runRecord(listings: readonly HostListing[], run: RunRef): RunRecord {
  const listing = listings.find(each => each.host === run.host);
  if (listing?.kind !== 'listed') return {kind: 'unlisted'};
  const live = newest(listing.live, run.runId);
  if (live !== undefined) return {kind: 'live', run: live};
  const ended = newest(listing.ended, run.runId);
  return ended === undefined ? {kind: 'unlisted'} : {kind: 'ended', run: ended};
}

/** Whether two records say the same thing, so an unchanged listing is not folded again. */
export function sameRecord(left: RunRecord, right: RunRecord): boolean {
  if (left.kind === 'unlisted' || right.kind === 'unlisted') return left.kind === right.kind;
  return left.kind === right.kind && left.run === right.run;
}
