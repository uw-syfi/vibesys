/** The journeys' world: a seeded Fake bridge, and opening the shell page over it. */

import {expect, type Page} from '@playwright/test';
import type {ChromeInset} from '@vibesys/ipc';
import {
  clusterTestsByAlias,
  loginStartsByProvider,
  machineSettingsWorld,
  storeRunFixture,
} from '@vibesys/run-fixtures';
import {
  type FakeBridgeSeed,
  type FakeNewRun,
  installFakeBridge,
  installFakeChrome,
  installFakeExampleDestination,
  installFakeMachineSettings,
  installFakeMachines,
  installFakeNewRun,
  installFakeProjectRuns,
  MAC_CHROME,
} from './fake-bridge.js';
import {installGateway, wire} from './fake-gateway.js';
import {FAKE_OFFER} from './fake-offer.js';
import {addInitScripts, initScript} from './init-scripts.js';
import {showScreen, sidebar, whenRead} from './read.js';
import {STARTED_AT} from './started-at.js';

export interface FakeHostWindow extends Window {
  fakeHost: {
    readonly newRun: FakeNewRun;
    listing(listing: unknown): void;
    readonly retried: readonly string[];
    openSettings(): void;
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

export async function open(page: Page, seed: FakeBridgeSeed = SEED): Promise<void> {
  await addInitScripts(page, [
    initScript(installFakeChrome, seed),
    initScript(installFakeBridge, seed),
    initScript(installFakeMachines, seed),
    initScript(installFakeMachineSettings, seed),
    initScript(installFakeNewRun, seed),
    initScript(installFakeProjectRuns, seed),
    initScript(installFakeExampleDestination, seed),
  ]);
  await page.goto('./shell.html');
  await expect(sidebar(page)).toBeVisible();
  await whenRead(page);
}

/** Goes to New run by the rail, as a user does, and returns once the screen has read what it shows. */
export const showNewRun = (page: Page): Promise<void> => showScreen(page, 'New');

/** Opens the app and goes to New run by the rail, as a user starts a run. */
export async function openNewRun(page: Page, seed: FakeBridgeSeed = SEED): Promise<void> {
  // The run that Start run opens is read over the gateway like every run.
  await installGateway(page, wire(storeRunFixture('live')));
  await open(page, seed);
  await showNewRun(page);
}

/**
 * What the host does when the window enters or leaves full screen: the controls' inset changes on
 * the root element, without a reload.
 */
export async function setChromeInset(page: Page, inset: ChromeInset): Promise<void> {
  await page.evaluate(next => {
    const root = document.documentElement;
    root.style.setProperty('--chrome-start-width', `${next.startWidth}px`);
    root.style.setProperty('--chrome-start-height', `${next.startHeight}px`);
    root.style.setProperty('--chrome-end-height', `${next.endHeight}px`);
  }, inset);
}
