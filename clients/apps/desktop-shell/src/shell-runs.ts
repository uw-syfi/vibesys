/**
 * The Runs dashboard's input from the app: what the shell knows of each run (`DashboardRow`, from
 * the hosts' listings and digests) as what the dashboard's tile fold takes (`RunInput`). The
 * dashboard feature knows no host, so this is the one place the two meet.
 */
import type {DashboardRow, RunRowStatus} from '@vibesys/feature-app-shell/state';
import {digestKey, formatStarted} from '@vibesys/feature-app-shell/state';
import type {RunInput, StatusKind} from '@vibesys/feature-runs-dashboard/state';
import type {HostKey} from '@vibesys/hosts';

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

/** How many runs the dashboard lists on each machine: what the Machines page says of each. */
export function runCounts(rows: readonly {readonly host: HostKey}[]): ReadonlyMap<HostKey, number> {
  const counts = new Map<HostKey, number>();
  for (const {host} of rows) counts.set(host, (counts.get(host) ?? 0) + 1);
  return counts;
}

function projectName(path: string): string {
  return path.split('/').filter(Boolean).at(-1) ?? path;
}

/** The seconds from `at`, in milliseconds, to `now`; null when `at` is not known. */
function secondsSince(at: number | null | undefined, now: number): number | null {
  return at == null ? null : Math.max(0, (now - at) / 1000);
}

/** The dashboard's input for `row`; `now` is the moment a live run's time so far is counted to. */
export function runInput(entry: DashboardRow, now: number): RunInput {
  const {row, digest} = {row: entry.row, digest: entry.row.digest};
  const dashboard = digest?.dashboard ?? null;
  const seconds = digest?.durationSeconds ?? secondsSince(row.startedAt, now);
  return {
    id: digestKey(row.run),
    runId: row.run.runId,
    title: entry.row.title,
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
    // A live run's `now` says what it does, not why it ended.
    reason: row.status === 'live' ? null : row.now,
    ideas: digest?.ideas ?? null,
    costUsd: null,
    seconds,
    best: digest === null ? null : (digest.adoptedRatio ?? digest.bestSoFarRatio),
    measured: dashboard?.best ?? null,
    createdAt: entry.createdAt,
    points: dashboard?.points ?? null,
    pointsOmitted: dashboard?.pointsOmitted ?? 0,
    attention: dashboard?.attention ?? 'none',
    failedStreak: dashboard?.failedStreak ?? 0,
    silentSeconds: secondsSince(dashboard?.lastEventAt, now),
  };
}
