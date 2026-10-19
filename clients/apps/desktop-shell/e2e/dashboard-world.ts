/**
 * Worlds for the Runs dashboard: runs on two machines, each with the digest a host that offers
 * `run_dashboard` would send (its ideas in order, and why it needs its user), so the tiles draw
 * their charts. `dashboardWorld(runs)` turns a list of run cases into the Fake bridge's seed;
 * `open` shows the dashboard on it.
 */
import {expect, type Page} from '@playwright/test';
import {
  type FakeBridgeSeed,
  installFakeBridge,
  installFakeDigests,
  installFakeMachineSettings,
  installFakeMachines,
  installFakeNewRun,
  installFakeProjectRuns,
} from './fake-bridge.js';
import {PROJECT, SEED} from './world.js';

export const NOW = Date.parse('2026-10-17T12:00:00Z');
const HOUR = 3_600_000;

type Verdict = 'kept' | 'not_better' | 'failed' | 'running';
type Phase = 'running' | 'paused' | 'completed' | 'failed' | 'stopped';
type Attention = 'none' | 'failed' | 'repeated_failure' | 'stalled';

/** One run of a world: where it is, how it stands and what its digest says. */
export interface RunCase {
  readonly runId: string;
  readonly host: 'ssh:host-a' | 'ssh:host-b';
  readonly task: string;
  readonly phase: Phase;
  /** Hours before now that the run began. */
  readonly ago: number;
  /** Each idea's result against the base (none before it has a reading) and verdict, in order. */
  readonly points: readonly (readonly [number | null, Verdict])[];
  readonly omitted?: number;
  readonly attention?: Attention;
  readonly adopted?: number | null;
  readonly minutes?: number;
}

const ENDS: Readonly<Record<'completed' | 'failed' | 'stopped', string>> = {
  completed: 'adopted',
  failed: 'failed',
  stopped: 'stopped',
};

function server(run: RunCase): Record<string, unknown> {
  const ended = run.phase === 'completed' || run.phase === 'failed' || run.phase === 'stopped';
  return {
    id: `0000000000${run.runId.slice(-2)}`,
    projectRoot: PROJECT,
    runId: run.runId,
    status: run.phase,
    phase: run.phase,
    paused: run.phase === 'paused' ? 'user' : null,
    end: ended
      ? {outcome: ENDS[run.phase as keyof typeof ENDS], reason: 'ended', resumable: false}
      : null,
    startedAt: Math.floor((NOW - run.ago * HOUR) / 1000),
    reason: null,
  };
}

function digest(run: RunCase): Record<string, unknown> {
  const over = run.phase === 'completed' || run.phase === 'failed' || run.phase === 'stopped';
  const kept = run.points.flatMap(([ratio, verdict]) =>
    verdict === 'kept' && ratio !== null ? [ratio] : [],
  );
  const best = kept.length === 0 ? null : Math.max(...kept);
  return {
    kind: 'read',
    createdAt: NOW - run.ago * HOUR,
    ideas: run.points.length + (run.omitted ?? 0),
    adoptedRatio: run.adopted === undefined ? (over ? best : null) : run.adopted,
    bestSoFarRatio: over ? null : best,
    durationSeconds: (run.minutes ?? 40) * 60,
    outcome: over ? ENDS[run.phase as keyof typeof ENDS] : null,
    closing: over ? 'finished' : null,
    over,
    dashboard: {
      points: run.points.map(([ratio, verdict]) => ({ratio, verdict})),
      pointsOmitted: run.omitted ?? 0,
      failedStreak: 0,
      lastEventAt: NOW,
      attention: run.attention ?? 'none',
    },
  };
}

/** What `host` lists of its servers for `runs`: those still running live, the rest ended. */
export function listingOf(
  runs: readonly RunCase[],
  host: RunCase['host'],
): Record<string, unknown> {
  const mine = runs.filter(run => run.host === host);
  const live = mine.filter(run => run.phase === 'running' || run.phase === 'paused');
  return {
    kind: 'listed',
    host,
    live: live.map(server),
    ended: mine.filter(run => !live.includes(run)).map(server),
    unreadable: [],
    unverified: [],
  };
}

/** The Fake bridge's seed for `runs`: their listings, digests and the two machines. */
export function dashboardWorld(
  runs: readonly RunCase[],
  digestless: readonly string[] = [],
): FakeBridgeSeed {
  const hosts = ['ssh:host-a', 'ssh:host-b'] as const;
  const listing = (host: (typeof hosts)[number]) => listingOf(runs, host);
  return {
    ...SEED,
    listings: hosts.map(listing),
    recent: [],
    machines: [
      {host: 'local', label: 'This Mac', checkout: '~/projs/vibesys'},
      {host: 'ssh:host-a', label: 'host-a', checkout: '/home/me/vibesys'},
      {host: 'ssh:host-b', label: 'host-b', checkout: '/home/me/vibesys'},
    ],
    projectRuns: Object.fromEntries(
      hosts.map(host => [
        `${host}|${PROJECT}`,
        runs
          .filter(run => run.host === host)
          .map(run => ({runId: run.runId, task: run.task, createdAt: NOW - run.ago * HOUR})),
      ]),
    ),
    digests: Object.fromEntries(runs.map(run => [run.runId, digest(run)])),
    digestless,
  };
}

/** Opens the app on `world` and waits for it to settle on the Runs dashboard. */
export async function open(page: Page, world: FakeBridgeSeed): Promise<void> {
  await page.clock.setFixedTime(NOW);
  await page.addInitScript(installFakeBridge, world);
  await page.addInitScript(installFakeMachines, world);
  await page.addInitScript(installFakeMachineSettings, world);
  await page.addInitScript(installFakeDigests, world);
  await page.addInitScript(installFakeProjectRuns, world);
  await page.addInitScript(installFakeNewRun, world);
  await page.goto('./shell.html');
  await expect(page.getByRole('heading', {name: 'Runs'})).toBeVisible();
}

const CLIMB = [
  [1.12, 'kept'],
  [0.96, 'not_better'],
  [null, 'failed'],
  [1.31, 'kept'],
  [1.18, 'not_better'],
  [1.42, 'kept'],
] as const;

export const ONE_RUN: readonly RunCase[] = [
  {
    runId: 'r-live-01',
    host: 'ssh:host-a',
    task: 'mpmc',
    phase: 'running',
    ago: 1,
    points: [...CLIMB, [null, 'running']],
  },
];

/** Runs of every status, three tasks and two machines, two of them needing the user. */
export const MANY_RUNS: readonly RunCase[] = [
  ...ONE_RUN,
  {
    runId: 'r-live-02',
    host: 'ssh:host-b',
    task: 'spsc',
    phase: 'running',
    ago: 2,
    points: [
      [1.05, 'kept'],
      [null, 'running'],
    ],
  },
  {
    runId: 'r-paused-03',
    host: 'ssh:host-a',
    task: 'mpsc',
    phase: 'paused',
    ago: 3,
    points: [
      [1.2, 'kept'],
      [1.1, 'not_better'],
    ],
  },
  {
    runId: 'r-done-04',
    host: 'ssh:host-a',
    task: 'mpmc',
    phase: 'completed',
    ago: 20,
    points: CLIMB,
  },
  {
    runId: 'r-done-05',
    host: 'ssh:host-b',
    task: 'mpmc',
    phase: 'completed',
    ago: 26,
    points: [
      [1.6, 'kept'],
      [1.9, 'kept'],
      [1.7, 'not_better'],
    ],
  },
  {
    runId: 'r-done-06',
    host: 'ssh:host-b',
    task: 'spsc',
    phase: 'completed',
    ago: 30,
    points: [[1.02, 'kept']],
  },
  {
    runId: 'r-failed-07',
    host: 'ssh:host-a',
    task: 'spsc',
    phase: 'failed',
    ago: 5,
    attention: 'repeated_failure',
    points: [
      [null, 'failed'],
      [null, 'failed'],
      [null, 'failed'],
    ],
  },
  {
    runId: 'r-stalled-08',
    host: 'ssh:host-b',
    task: 'mpsc',
    phase: 'running',
    ago: 8,
    attention: 'stalled',
    points: [
      [1.3, 'kept'],
      [1.25, 'not_better'],
    ],
  },
  {
    runId: 'r-stopped-09',
    host: 'ssh:host-a',
    task: 'mpsc',
    phase: 'stopped',
    ago: 40,
    points: [[0.9, 'not_better']],
  },
];

/** A run with more ideas than a chart keeps: 64 points and 36 left out. */
export const OMITTED_RUN: readonly RunCase[] = [
  {
    runId: 'r-long-01',
    host: 'ssh:host-a',
    task: 'mpmc',
    phase: 'running',
    ago: 6,
    omitted: 36,
    points: Array.from({length: 64}, (_, index): readonly [number | null, Verdict] => {
      const step = index % 5;
      if (step === 2) return [null, 'failed'];
      if (step === 4) return [1 + (index % 17) * 0.01, 'not_better'];
      return [1 + index * 0.012, 'kept'];
    }),
  },
];
