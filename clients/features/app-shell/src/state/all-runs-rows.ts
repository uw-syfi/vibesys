/**
 * The rows of the Runs dashboard: the newest runs the connected machines list, newest first across
 * machines (at most `DASHBOARD_RUNS`, none archived). A row is the sidebar's own (`RunRow`): a live
 * run's, from its server, and any other run's as a Recent row reads it, from the registry's record
 * of its end and its digest.
 */
import {type HostKey, hostFromKey, hostLabel} from '@vibesys/hosts';
import {type RecentRun, sameRun} from '@vibesys/runs';
import type {Clock} from './recent-words.js';
import {dashboardRefs, liveRows, type RunRow, recentRow, type SidebarState} from './sidebar.js';

/** One run of the dashboard: its row, and what the machine's listing says of it. */
export interface DashboardRow {
  readonly row: RunRow;
  readonly host: HostKey;
  /** The machine's name, as the app words it. */
  readonly machine: string;
  /** The run's own name. */
  readonly name: string;
  /** The task the run ran, or null when the run has no record of one. */
  readonly task: string | null;
  /** The project's path on the machine. */
  readonly project: string;
  /** When the run was created, in milliseconds. */
  readonly createdAt: number;
}

/** The runs the dashboard shows, newest first. */
export function dashboardRows(state: SidebarState, clock: Clock): readonly DashboardRow[] {
  const live = liveRows(state);
  return dashboardRefs(state).map(({host, project, run}) => {
    const machine = hostLabel(hostFromKey(host));
    const ref: RecentRun = {
      host,
      hostLabel: machine,
      project,
      task:
        run.task ??
        state.recent.find(each => sameRun(each, {host, runId: run.runId}))?.task ??
        null,
      runId: run.runId,
      instanceId: null,
      attachedAt: run.createdAt,
    };
    const row = live.find(each => sameRun(each.run, ref)) ?? recentRow(state, ref, clock);
    return {
      row,
      host,
      machine,
      name: run.name ?? run.runId,
      task: ref.task,
      project,
      createdAt: run.createdAt,
    };
  });
}
