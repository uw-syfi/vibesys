/**
 * The run's record in its host's latest listing: the newest live server driving it, else the newest
 * tombstone it left, else a saved run when the listing can prove no server holds it, else unlisted
 * with the reason it cannot be placed (`UnlistedWhy`).
 *
 * A run opened without a recent entry (from the host's own run list) whose tombstone expired is
 * found in neither of the listing's two lists; the listing proves it is not live when it holds no
 * row that could be its server (`unverified`, `unreadable`).
 */
import type {ConnectionStatus, HostListing, HostRun} from '@vibesys/hosts';
import {type RecentRun, type RunRef, recentStatus} from '@vibesys/runs';
import type {MachineBlock, RunRecord} from './run-page.js';

/** Why `connection` will not list runs without someone acting, or null while it may yet. */
function blockOf(connection: ConnectionStatus): MachineBlock | null {
  switch (connection.kind) {
    case 'incompatible':
      return 'needs-update';
    case 'auth-needed':
      return 'sign-in';
    case 'offline':
      return 'offline';
    case 'closed':
      return 'closed';
    default:
      return null;
  }
}

const MACHINE: RunRecord = {kind: 'unlisted', why: 'machine'};
const UNCONFIRMED: RunRecord = {kind: 'unlisted', why: 'unconfirmed'};

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
  if (listing?.kind !== 'listed') {
    const block = listing === undefined ? null : blockOf(listing.connection);
    return block === null ? MACHINE : {kind: 'unlisted', why: 'machine', block};
  }
  const live = newest(listing.live, run.runId, recent?.project);
  if (live !== undefined) return {kind: 'live', run: live};
  const ended = newest(listing.ended, run.runId, recent?.project);
  if (ended !== undefined) return {kind: 'ended', run: ended};
  const uncertain = listing.unverified.length > 0 || listing.unreadable.length > 0;
  if (recent === undefined) return uncertain ? UNCONFIRMED : {kind: 'historical', status: null};
  const status = recentStatus(recent, listing);
  if (status.kind !== 'ended') return UNCONFIRMED;
  const phase = status.status;
  return {
    kind: 'historical',
    status: phase === 'stopped' || phase === 'completed' || phase === 'failed' ? phase : null,
  };
}

/** Whether two records say the same thing, so an unchanged listing is not folded again. */
export function sameRecord(left: RunRecord, right: RunRecord): boolean {
  if (left.kind === 'unlisted')
    return right.kind === 'unlisted' && left.why === right.why && left.block === right.block;
  if (right.kind === 'unlisted') return false;
  if (left.kind === 'historical')
    return right.kind === 'historical' && left.status === right.status;
  if (right.kind === 'historical') return false;
  return left.kind === right.kind && left.run === right.run;
}
