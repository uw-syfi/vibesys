/** The journeys' world: a seeded Fake bridge, and opening the shell page over it. */
import {expect, type Page} from '@playwright/test';
import {
  type FakeBridgeSeed,
  type FakeNewRun,
  installFakeBridge,
  installFakeMachines,
  installFakeNewRun,
} from './fake-bridge.js';

export interface FakeHostWindow extends Window {
  fakeHost: {
    readonly newRun: FakeNewRun;
    listing(listing: unknown): void;
    recent(runs: readonly unknown[]): void;
  };
}

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
    startedAt: 1,
    reason: null,
  };
}

export function listed(live: unknown[], ended: unknown[]): Record<string, unknown> {
  return {kind: 'listed', host: 'ssh:chelan4', live, ended, unreadable: [], unverified: []};
}

const RECENT = [
  {runId: '20261015-163047', task: 'mpmc', attachedAt: 3},
  {runId: '20261014-090000', task: 'spsc', attachedAt: 2},
  {runId: '20261013-120000', task: null, attachedAt: 1},
].map(run => ({
  host: 'ssh:chelan4',
  hostLabel: 'chelan4',
  project: PROJECT,
  instanceId: null,
  ...run,
}));

export const SEED: FakeBridgeSeed = {
  theme: 'paper',
  // macOS's traffic lights (`chromeInset('darwin')` in the desktop app's window chrome).
  chromeInset: {width: 84, height: 40},
  appearance: {mode: 'system', light: 'paper', dark: 'ink'},
  listings: [
    listed(
      [server('00000000000a', '20261015-163047', 'running')],
      [server('00000000000b', '20261014-090000', 'failed')],
    ),
  ],
  recent: RECENT,
  machines: [
    {host: 'local', label: 'This Mac', checkout: '~/projs/vibesys'},
    {host: 'ssh:chelan4', label: 'chelan4', checkout: '/home/me/vibesys'},
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
  newRun: {
    machine: {
      checkout: {root: '/home/me/vibesys', version: '0.9.0', commit: 'cc65df3', dirty: false},
      hardware: {
        cpu_cores: 64,
        gpus: [{vendor: 'amd', model: 'MI300X'}],
        accelerator: 'rocm',
      },
      docker: {reachable: true, version: '27.3.1'},
      harness_clis: ['claude', 'codex'],
      runs: {live: 1, ended_today: 2},
    },
    harnesses: [
      {provider: 'claude', model: 'claude-opus-4-5', default: true},
      {provider: 'claude', model: 'claude-sonnet-4-5', default: false},
      {provider: 'codex', model: 'gpt-5-codex', default: true},
    ],
    examples: {
      checkout: '/home/me/vibesys',
      examples: [
        {name: 'queue-rs', category: 'rust', tasks: ['mpmc', 'mpsc', 'spsc'], external_repo: false},
        {name: 'kv-store', category: 'rust', tasks: ['get-put'], external_repo: false},
      ],
    },
    projects: {
      [PROJECT]: ['mpmc', 'mpsc', 'spsc'],
      '/home/me/vibesys/examples/queue-rs': ['mpmc', 'mpsc', 'spsc'],
    },
  },
};

export async function open(page: Page, seed: FakeBridgeSeed = SEED): Promise<void> {
  await page.addInitScript(installFakeBridge, seed);
  await page.addInitScript(installFakeMachines, seed);
  await page.addInitScript(installFakeNewRun, seed);
  await page.goto('./shell.html');
  await expect(page.getByRole('navigation', {name: 'VibeSys'})).toBeVisible();
}

export const sidebar = (page: Page) => page.getByRole('navigation', {name: 'VibeSys'});
