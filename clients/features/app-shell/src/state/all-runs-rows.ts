/**
 * The rows of the Runs dashboard: the newest runs the connected machines list, newest first across
 * machines (at most `DASHBOARD_RUNS`, none archived). A row is the sidebar's own (`RunRow`): a live
 * run's, from its server, and any other run's as a Recent row reads it, from the registry's record
 * of its end and its digest.
 */
import {type HostKey, hostFromKey, hostLabel} from '@vibesys/hosts';
import {type RecentRun, sameRun} from '@vibesys/runs';
import {type Clock, formatStarted} from './recent-words.js';
import {
  dashboardRefs,
  liveRows,
  type RunRow,
  recentRow,
  type SidebarState,
  titleOf,
} from './sidebar.js';

/** One run of the dashboard: its row, and what the machine's listing says of it. */
export interface DashboardRow {
  readonly row: RunRow;
  readonly host: HostKey;
  /** The machine's name, as the app words it. */
  readonly machine: string;
  /** The task the machine lists the run with, else its Recent record's; null when neither has one. */
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
      // The task the user chose, which titles the run on every screen; a listing's task cannot say
      // whether anyone chose it (a project made from flags has one task, `default`).
      task: state.recent.find(each => sameRun(each, {host, runId: run.runId}))?.task ?? null,
      runId: run.runId,
      instanceId: null,
      attachedAt: run.createdAt,
    };
    const row = live.find(each => sameRun(each.run, ref)) ?? recentRow(state, ref, clock);
    return {
      row,
      host,
      machine,
      task: run.task ?? ref.task,
      project,
      createdAt: run.createdAt,
    };
  });
}

/** What a screen that lists runs by cost (Usage) calls one: its title, and when it began if the app knows. */
export interface RunLabel {
  readonly title: string;
  readonly began: string | null;
}

/**
 * The label of any run, by the same title every other screen gives it. When it began is known for
 * the runs the dashboard lists (the machine's listing says); null for older ones.
 */
export function runLabels(
  state: SidebarState,
  clock: Clock,
): (run: {readonly host: HostKey; readonly project: string; readonly runId: string}) => RunLabel {
  const created = new Map(
    dashboardRefs(state).map(({host, run}) => [JSON.stringify([host, run.runId]), run.createdAt]),
  );
  return ({host, project, runId}) => {
    const at = created.get(JSON.stringify([host, runId]));
    return {
      title: titleOf(state, {host, runId}, project),
      began: at === undefined ? null : formatStarted(at, clock),
    };
  };
}
