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
import {addInitScripts, initScript} from './init-scripts.js';
import {SEED} from './world.js';

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
  /** The run's own name, also its project's folder; its id when none is given. */
  readonly name?: string;
  /** What the machine says it ended with, in its journal's own words. */
  readonly endSummary?: string;
  /** The best idea's value in the run's own metric, when the host reads it. */
  readonly best?: {
    readonly value: number;
    readonly metric: string | null;
    readonly unit: string | null;
  };
  /** Minutes the journal has been silent. */
  readonly silent?: number;
  readonly attention?: Attention;
  readonly adopted?: number | null;
  readonly minutes?: number;
}

const ENDS: Readonly<Record<'completed' | 'failed' | 'stopped', string>> = {
  completed: 'adopted',
  failed: 'failed',
  stopped: 'stopped',
};

/** A run's project: one folder per run, named as the run is, so every tile has its own title. */
function projectOf(run: RunCase): string {
  return `/home/me/projects/${run.name ?? run.runId}`;
}

function server(run: RunCase): Record<string, unknown> {
  const ended = run.phase === 'completed' || run.phase === 'failed' || run.phase === 'stopped';
  return {
    id: `0000000000${run.runId.slice(-2)}`,
    projectRoot: projectOf(run),
    runId: run.runId,
    status: run.phase,
    phase: run.phase,
    paused: run.phase === 'paused' ? 'user' : null,
    end: ended
      ? {
          outcome: ENDS[run.phase as keyof typeof ENDS],
          reason: run.endSummary ?? 'ended',
          resumable: false,
        }
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
    ...(run.endSummary === undefined ? {} : {endSummary: run.endSummary}),
    dashboard: {
      points: run.points.map(([ratio, verdict]) => ({ratio, verdict})),
      pointsOmitted: run.omitted ?? 0,
      failedStreak: 0,
      lastEventAt: NOW - (run.silent ?? 0) * 60_000,
      attention: run.attention ?? 'none',
      best: run.best ?? null,
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
    // The user opened every run, so each has the task it was started for.
    recent: runs.map(run => ({
      host: run.host,
      hostLabel: run.host,
      project: projectOf(run),
      task: run.task,
      runId: run.runId,
      instanceId: null,
      attachedAt: 1,
    })),
    machines: [
      {host: 'local', label: 'This Mac', checkout: '~/projs/vibesys'},
      {host: 'ssh:host-a', label: 'host-a', checkout: '/home/me/vibesys'},
      {host: 'ssh:host-b', label: 'host-b', checkout: '/home/me/vibesys'},
    ],
    projectRuns: Object.fromEntries(
      runs.map(run => [
        `${run.host}|${projectOf(run)}`,
        [
          {
            runId: run.runId,
            name: run.name ?? run.runId,
            task: run.task,
            createdAt: NOW - run.ago * HOUR,
          },
        ],
      ]),
    ),
    digests: Object.fromEntries(runs.map(run => [run.runId, digest(run)])),
    digestless,
  };
}

/** Opens the app on `world` and waits for it to settle on the Runs dashboard. */
export async function open(page: Page, world: FakeBridgeSeed): Promise<void> {
  await page.clock.setFixedTime(NOW);
  await addInitScripts(page, [
    initScript(installFakeBridge, world),
    initScript(installFakeMachines, world),
    initScript(installFakeMachineSettings, world),
    initScript(installFakeDigests, world),
    initScript(installFakeProjectRuns, world),
    initScript(installFakeNewRun, world),
  ]);
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
    name: 'queue-rs-run1',
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
    name: 'queue-rs-run2',
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
    name: 'queue-rs-run3',
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
    name: 'queue-rs-run4',
    host: 'ssh:host-a',
    task: 'mpmc',
    phase: 'completed',
    ago: 20,
    points: CLIMB,
  },
  {
    runId: 'r-done-05',
    name: 'queue-rs-run5',
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
    name: 'queue-rs-run6',
    host: 'ssh:host-b',
    task: 'spsc',
    phase: 'completed',
    ago: 30,
    points: [[1.02, 'kept']],
  },
  {
    runId: 'r-failed-07',
    name: 'queue-rs-run7',
    host: 'ssh:host-a',
    task: 'spsc',
    phase: 'failed',
    ago: 5,
    attention: 'failed',
    points: [
      [null, 'failed'],
      [null, 'failed'],
      [null, 'failed'],
    ],
  },
  {
    runId: 'r-stalled-08',
    name: 'queue-rs-run8',
    host: 'ssh:host-b',
    task: 'mpsc',
    phase: 'running',
    ago: 8,
    attention: 'stalled',
    silent: 32,
    points: [
      [1.3, 'kept'],
      [1.25, 'not_better'],
    ],
  },
  {
    runId: 'r-stopped-09',
    name: 'queue-rs-run9',
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
    name: 'queue-rs-run1',
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

/** A live run whose input was never measured, and runs that failed with long reasons. */
export const MEASURED_AND_FAILED: readonly RunCase[] = [
  {
    runId: 'r-live-measured',
    name: 'queue-rs-run1',
    host: 'ssh:host-a',
    task: 'mpmc',
    phase: 'running',
    ago: 1,
    points: [
      [null, 'kept'],
      [null, 'running'],
    ],
    best: {value: 287.4, metric: null, unit: 'tok/s'},
  },
  {
    runId: 'r-failed-class',
    name: 'queue-rs-run2',
    host: 'ssh:host-a',
    task: 'mpmc',
    phase: 'failed',
    ago: 5,
    attention: 'failed',
    endSummary: 'NestedRepositoryError: before the run could begin it found a repository inside',
    points: [[null, 'failed']],
  },
  {
    runId: 'r-failed-prose',
    name: 'queue-rs-run3',
    host: 'ssh:host-a',
    task: 'mpmc',
    phase: 'failed',
    ago: 6,
    attention: 'failed',
    endSummary: 'the agent was interrupted because the machine went away during the second round',
    points: [[null, 'failed']],
  },
];
