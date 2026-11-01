/**
 * The world every journey starts from: a seeded Fake bridge's data (one live and one failed run on
 * `host-b`, three recent runs, two machines, one project with three tasks). It imports nothing of
 * Playwright, so the DOM tests of the shell draw the same world as the browser journeys.
 */
import {
  clusterTestsByAlias,
  loginStartsByProvider,
  machineSettingsWorld,
} from '@vibesys/run-fixtures';
import {type FakeBridgeSeed, MAC_CHROME} from './fake-bridge.js';
import {FAKE_OFFER} from './fake-offer.js';
import {STARTED_AT} from './started-at.js';

export const PROJECT = '/home/me/projects/queue-rs';

export function server(id: string, runId: string, phase: string): Record<string, unknown> {
  const end =
    phase === 'completed'
      ? {outcome: 'adopted', reason: 'adopted the best', resumable: false}
      : null;
  const paused = phase === 'paused' ? 'user' : null;
  return {
    id,
    projectRoot: PROJECT,
    runId,
    status: phase,
    phase,
    paused,
    end,
    startedAt: STARTED_AT,
    reason: null,
  };
}

export function listed(live: unknown[], ended: unknown[]): Record<string, unknown> {
  return {kind: 'listed', host: 'ssh:host-b', live, ended, unreadable: [], unverified: []};
}

const RECENT = [
  {runId: '20261015-163047', task: 'mpmc', attachedAt: 3},
  {runId: '20261014-090000', task: 'spsc', attachedAt: 2},
  {runId: '20261013-120000', task: null, attachedAt: 1},
].map(run => ({
  host: 'ssh:host-b',
  hostLabel: 'host-b',
  project: PROJECT,
  instanceId: null,
  ...run,
}));

export const SEED: FakeBridgeSeed = {
  theme: 'paper',
  chromeInset: MAC_CHROME,
  appearance: {mode: 'system', light: 'paper', dark: 'ink'},
  listings: [
    listed(
      [server('00000000000a', '20261015-163047', 'running')],
      [server('00000000000b', '20261014-090000', 'failed')],
    ),
  ],
  recent: RECENT,
  projectRuns: {
    [`ssh:host-b|${PROJECT}`]: RECENT.map(({runId, task, attachedAt}) => ({
      runId,
      task,
      createdAt: attachedAt,
    })),
  },
  machines: [
    {host: 'local', label: 'This Mac', checkout: '~/projs/vibesys'},
    {host: 'ssh:host-b', label: 'host-b', checkout: '/home/me/vibesys'},
  ],
  directories: {
    '/home/me/': [
      {name: 'notes', path: '/home/me/notes', checkout: false, project: false},
      {name: 'projects', path: '/home/me/projects', checkout: false, project: false},
    ],
    '/home/me/projects/': [
      {name: 'queue-rs', path: '/home/me/projects/queue-rs', checkout: false, project: true},
    ],
  },
  machineSettings: machineSettingsWorld,
  clusterTests: clusterTestsByAlias,
  loginStarts: loginStartsByProvider,
  newRun: {
    ...FAKE_OFFER,
    projects: {
      [PROJECT]: ['mpmc', 'mpsc', 'spsc'],
      '/home/me/vibesys/examples/queue-rs': ['mpmc', 'mpsc', 'spsc'],
    },
    goals: Object.fromEntries(
      ['mpmc', 'mpsc', 'spsc'].map(task => [
        `${PROJECT} ${task}`,
        {
          task,
          objectives: [
            {metric: 'ops_per_second', direction: 'max'},
            {metric: 'p99_latency_us', direction: 'min'},
          ],
          goal: `# Make the ${task} queue faster\n\nRaise throughput on the \`${task}\` workload without breaking linearizability.\n\n- Keep the public API.\n- Report ops per second.`,
          goal_truncated: false,
        },
      ]),
    ),
  },
};
