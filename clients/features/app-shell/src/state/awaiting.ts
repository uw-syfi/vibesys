/**
 * Whether the sidebar still waits for a reply that changes what the shell shows: the overview (the
 * first listings and Recent), a project's runs, a Recent run's digest, a live run's first digest.
 * A live run's digest read again is not waited for: what it shows is already there, and it is
 * read again for as long as the run works.
 */
import type {SidebarState} from './sidebar.js';

export function sidebarAwaiting(state: SidebarState): boolean {
  return (
    state.overview.kind === 'waiting' ||
    Object.values(state.allRuns.projects).some(slot => slot.kind === 'loading') ||
    Object.values(state.digests).some(slot => slot.kind === 'loading') ||
    Object.values(state.live).some(digest => digest.entry === null && digest.token !== null)
  );
}
