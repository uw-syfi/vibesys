/**
 * The rows of the All runs screen: every run the connected machines list, grouped by machine, newest
 * first. A row is the sidebar's own (`RunRow`): a live run's, from its server, and any other run's as
 * a Recent row reads it, from the registry's record of its end and its digest.
 */
import {type HostKey, hostFromKey, hostLabel} from '@vibesys/hosts';
import {type RecentRun, sameRun} from '@vibesys/runs';
import {hostSlots, listedRunRefs} from './all-runs.js';
import type {Clock} from './recent-words.js';
import {liveRows, type RunRow, recentRow, type SidebarState} from './sidebar.js';

/** What one machine's part of the screen says besides its rows. */
export type MachineRunsState =
  /** Some project's runs are still being listed. */
  | 'loading'
  /** Every project listed; the rows are all there is. */
  | 'listed'
  /** A project's runs could not be listed: asking again may help. */
  | 'failed';

/** One machine's runs, newest first. */
export interface AllRunsGroup {
  readonly host: HostKey;
  readonly title: string;
  readonly rows: readonly RunRow[];
  readonly state: MachineRunsState;
}

/** The runs of every connected machine, grouped by machine in listing order, newest first within each. */
export function allRunsGroups(state: SidebarState, clock: Clock): readonly AllRunsGroup[] {
  const live = liveRows(state);
  return state.listings.flatMap(listing => {
    if (listing.kind !== 'listed') return [];
    const mine = listedRunRefs(state.allRuns)
      .filter(entry => entry.host === listing.host)
      .sort(
        (left, right) =>
          right.run.createdAt - left.run.createdAt || (left.run.runId < right.run.runId ? 1 : -1),
      );
    const rows = mine.map(({host, project, run}) => {
      const ref: RecentRun = {
        host,
        hostLabel: hostLabel(hostFromKey(host)),
        project,
        task: run.task,
        runId: run.runId,
        instanceId: null,
        attachedAt: run.createdAt,
      };
      return live.find(row => sameRun(row.run, ref)) ?? recentRow(state, ref, clock);
    });
    return [
      {
        host: listing.host,
        title: hostLabel(hostFromKey(listing.host)),
        rows,
        state: machineState(hostSlots(state.allRuns, listing.host).map(slot => slot.kind)),
      },
    ];
  });
}

function machineState(kinds: readonly string[]): MachineRunsState {
  if (kinds.includes('loading')) return 'loading';
  return kinds.includes('failed') ? 'failed' : 'listed';
}
