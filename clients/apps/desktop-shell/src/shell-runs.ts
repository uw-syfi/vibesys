/**
 * The Runs dashboard's input from the app: what the shell knows of each run (`DashboardRow`, from
 * the hosts' listings and digests) as what the dashboard's tile fold takes (`RunInput`). The
 * dashboard feature knows no host, so this is the one place the two meet.
 */
import type {DashboardRow, RunRowStatus} from '@vibesys/feature-app-shell/state';
import {digestKey, formatStarted} from '@vibesys/feature-app-shell/state';
import type {RunInput, StatusKind} from '@vibesys/feature-runs-dashboard/state';

const STATUS_KINDS: Readonly<Record<RunRowStatus, StatusKind>> = {
  live: 'running',
  paused: 'paused',
  finished: 'finished',
  failed: 'failed',
  stopped: 'stopped',
  unknown: 'unknown',
};

const STATUS_WORDS: Readonly<Record<RunRowStatus, string>> = {
  live: 'running',
  paused: 'paused',
  finished: 'finished',
  failed: 'failed',
  stopped: 'stopped',
  unknown: 'unknown',
};

function projectName(path: string): string {
  return path.split('/').filter(Boolean).at(-1) ?? path;
}

/** The dashboard's input for `row`; `now` is the moment a live run's time so far is counted to. */
export function runInput(entry: DashboardRow, now: number): RunInput {
  const {row, digest} = {row: entry.row, digest: entry.row.digest};
  const dashboard = digest?.dashboard ?? null;
  const seconds =
    digest?.durationSeconds ??
    (row.startedAt === null ? null : Math.max(0, (now - row.startedAt) / 1000));
  return {
    id: digestKey(row.run),
    name: entry.name,
    task: entry.task ?? projectName(entry.project),
    started: formatStarted(entry.createdAt, {now}),
    machine: entry.machine,
    hardware: null,
    agent: null,
    status: {
      kind: STATUS_KINDS[row.status],
      word: row.problem === null ? row.look.word : STATUS_WORDS[row.status],
    },
    // A machine's trouble is a note beside the run's own status, never its replacement.
    problem: row.problem?.text ?? null,
    ideas: digest?.ideas ?? null,
    costUsd: null,
    seconds,
    best: digest === null ? null : (digest.adoptedRatio ?? digest.bestSoFarRatio),
    createdAt: entry.createdAt,
    points: dashboard?.points ?? null,
    pointsOmitted: dashboard?.pointsOmitted ?? 0,
    attention: dashboard?.attention ?? 'none',
    failedStreak: dashboard?.failedStreak ?? 0,
    silentSeconds:
      dashboard?.lastEventAt == null ? null : Math.max(0, (now - dashboard.lastEventAt) / 1000),
  };
}
