/**
 * The rows of the Runs dashboard: the newest runs the connected machines list, newest first across
 * machines (at most `DASHBOARD_RUNS`, none archived). A row is the sidebar's own (`RunRow`): a live
 * run's, from its server, and any other run's as a Recent row reads it, from the registry's record
 * of its end and its digest.
 */
import {isSynthesizedTask} from '@vibesys/contracts';
import {type HostKey, hostFromKey, hostLabel} from '@vibesys/hosts';
import {type RecentRun, sameRun} from '@vibesys/runs';
import {type Clock, formatStarted} from './recent-words.js';
import {
  DASHBOARD_RUNS,
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
  /**
   * The task the machine lists the run with, else its Recent record's; null when neither has one
   * or the only one is the name of a project made from flags.
   */
  readonly task: string | null;
  /** The project's path on the machine. */
  readonly project: string | null;
  /** When the run was created, in milliseconds. */
  readonly createdAt: number;
}

/** `task` when someone chose it: the one task of a project made from flags is nobody's choice. */
function chosenTask(task: string | null): string | null {
  return isSynthesizedTask(task) ? null : task;
}

/** The runs the dashboard shows, newest first. */
export function dashboardRows(state: SidebarState, clock: Clock): readonly DashboardRow[] {
  const live = liveRows(state);
  const known = dashboardRefs(state).map(({host, project, run}) => {
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
      row:
        run.unavailable === undefined
          ? row
          : {...row, detail: run.unavailable, problem: {text: run.unavailable, retry: false}},
      host,
      machine,
      task: chosenTask(run.task) ?? chosenTask(ref.task),
      project,
      createdAt: run.createdAt,
    };
  });
  const seen = new Set(known.map(each => JSON.stringify(each.row.run)));
  const unknown: DashboardRow[] = state.listings.flatMap(listing => {
    if (listing.kind !== 'listed') return [];
    return (listing.unknownProject ?? []).flatMap(run => {
      const ref = {host: listing.host, runId: run.runId};
      if (seen.has(JSON.stringify(ref)) || state.archived.some(each => sameRun(each, ref)))
        return [];
      const machine = hostLabel(hostFromKey(listing.host));
      return [
        {
          host: listing.host,
          machine,
          task: null,
          project: null,
          createdAt: run.startedAt * 1000,
          row: {
            run: ref,
            title: 'Unknown project',
            detail: 'Project unknown',
            trail: null,
            tooltip: `${machine}: ${run.runId}: Project unknown`,
            status: 'unknown',
            look: {tone: 'neutral', word: 'Project unknown'},
            problem: {text: 'Project unknown', retry: false},
            now: null,
            startedAt: run.startedAt === 0 ? null : run.startedAt * 1000,
            digest: null,
          },
        },
      ];
    });
  });
  return [...known, ...unknown].sort((a, b) => b.createdAt - a.createdAt).slice(0, DASHBOARD_RUNS);
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
): (run: {
  readonly host: HostKey;
  readonly project: string | null;
  readonly runId: string;
}) => RunLabel {
  const created = new Map(
    dashboardRefs(state).map(({host, run}) => [JSON.stringify([host, run.runId]), run.createdAt]),
  );
  return ({host, project, runId}) => {
    const at = created.get(JSON.stringify([host, runId]));
    return {
      title: project === null ? 'Unknown project' : titleOf(state, {host, runId}, project),
      began: at === undefined ? null : formatStarted(at, clock),
    };
  };
}
