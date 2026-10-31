/**
 * Whether the app knows of any run, which decides its home: with none on any machine the home is
 * New run; once one is listed, Runs is. While the machines are still answering the question is
 * open, so the shell keeps its screen instead of showing New run for a moment and then leaving it.
 * And where each machine's run list stands, which the Runs screen shows until it has tiles.
 */
import {
  failureWords,
  type HostKey,
  hostKey,
  LISTING_STEPS,
  type MachineLoad,
  machineLoad,
} from '@vibesys/hosts';
import {sameRun} from '@vibesys/runs';
import {projectRunsLoading} from './all-runs.js';
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
  if (state.overview.kind !== 'heard' || machines === null) return 'unknown';
  if (machines.some(host => !state.listings.some(listing => listing.host === host))) {
    return 'unknown';
  }
  if (Object.values(state.allRuns.projects).some(slot => slot.kind === 'failed')) return 'some';
  const loading = Object.values(state.allRuns.projects).some(slot => slot.kind === 'loading');
  return loading ? 'unknown' : 'none';
}

/** The machines to show the load of: This Mac, then the saved ones, or what has listed before those are read. */
function loadHosts(state: SidebarState, machines: readonly HostKey[] | null): HostKey[] {
  const named = machines ?? state.listings.map(listing => listing.host);
  return [...new Set([hostKey({kind: 'local'}), ...named])];
}

/**
 * Where each machine's run list stands. A machine stays on the last step until the projects it
 * lists are read, and a machine with nothing to say because the overview failed has failed.
 * `machines` is null while the saved machines are not read, or could not be.
 */
export function machineLoads(
  state: SidebarState,
  machines: readonly HostKey[] | null,
): MachineLoad[] {
  return loadHosts(state, machines).map(host => {
    const listing = state.listings.find(each => each.host === host);
    if (listing === undefined && state.overview.kind === 'failed') {
      const reason = failureWords(state.overview.reason);
      return {kind: 'failed', host, step: LISTING_STEPS[0], reason};
    }
    const load = machineLoad(host, listing);
    return load.kind === 'listed' && projectRunsLoading(state.allRuns, host)
      ? {kind: 'loading', host, step: 'runs', attempt: 0}
      : load;
  });
}
