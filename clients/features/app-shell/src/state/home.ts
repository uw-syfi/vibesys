/**
 * Whether the app knows of any run, which decides its home: with none on any machine the home is
 * New run and the rail has no Runs; once one is listed, Runs appears and is the home. While the
 * machines are still answering the question is open, so the shell keeps its screen instead of
 * showing New run for a moment and then leaving it.
 */
import type {HostKey} from '@vibesys/hosts';
import {dashboardRefs, liveRows, type SidebarState} from './sidebar.js';

export type RunsKnown = 'unknown' | 'none' | 'some';

/**
 * What the listings say of runs. Any listed run settles it as `some`; `none` needs the overview
 * heard, the saved `machines` read and each of them listed, and no project's runs still loading.
 * `machines` is null while the saved machines are not read yet.
 */
export function runsKnown(state: SidebarState, machines: readonly HostKey[] | null): RunsKnown {
  if (dashboardRefs(state).length > 0 || liveRows(state).length > 0) return 'some';
  if (!state.overviewHeard || machines === null) return 'unknown';
  if (machines.some(host => !state.listings.some(listing => listing.host === host))) {
    return 'unknown';
  }
  const loading = Object.values(state.allRuns.projects).some(slot => slot.kind === 'loading');
  return loading ? 'unknown' : 'none';
}
