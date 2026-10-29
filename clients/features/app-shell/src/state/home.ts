/**
 * Whether the app knows of any run, which decides its home: with none on any machine the home is
 * New run and the rail has no Runs; once one is listed, Runs appears and is the home. While the
 * machines are still answering the question is open, so the shell keeps its screen instead of
 * showing New run for a moment and then leaving it.
 */
import type {HostKey} from '@vibesys/hosts';
import {sameRun} from '@vibesys/runs';
import {dashboardRefs, liveRows, type SidebarState} from './sidebar.js';

export type RunsKnown = 'unknown' | 'none' | 'some';

/**
 * What the listings say of runs. Any listed run settles it as `some`; `none` needs the overview
 * heard, the saved `machines` read and each of them listed, and no project's runs still loading.
 * `machines` is null while the saved machines are not read yet.
 */
export function runsKnown(state: SidebarState, machines: readonly HostKey[] | null): RunsKnown {
  const known = listedRunsKnown(state, machines);
  // Runs that were shown are not undone by a question reopening (a machine that stopped answering,
  // a listing asked again): only a settled answer of none sends the shell to New run.
  return known === 'unknown' && state.runsSeen ? 'some' : known;
}

function listedRunsKnown(state: SidebarState, machines: readonly HostKey[] | null): RunsKnown {
  if (
    state.listings.some(
      listing =>
        listing.kind === 'listed' &&
        listing.unknownProject?.some(
          run =>
            !state.archived.some(each => sameRun(each, {host: listing.host, runId: run.runId})),
        ),
    )
  )
    return 'some';
  if (dashboardRefs(state).length > 0 || liveRows(state).length > 0) return 'some';
  if (!state.overviewHeard || machines === null) return 'unknown';
  if (machines.some(host => !state.listings.some(listing => listing.host === host))) {
    return 'unknown';
  }
  if (Object.values(state.allRuns.projects).some(slot => slot.kind === 'failed')) return 'some';
  const loading = Object.values(state.allRuns.projects).some(slot => slot.kind === 'loading');
  return loading ? 'unknown' : 'none';
}
