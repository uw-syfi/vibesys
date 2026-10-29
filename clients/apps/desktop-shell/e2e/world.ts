/** The journeys' world: a seeded Fake bridge, and opening the shell page over it. */

import {expect, type Page} from '@playwright/test';
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
  installFakeExampleDestination,
  installFakeMachineSettings,
  installFakeMachines,
  installFakeNewRun,
  installFakeProjectRuns,
} from './fake-bridge.js';
import {installGateway, wire} from './fake-gateway.js';
import {FAKE_OFFER} from './fake-offer.js';
import {addInitScripts, initScript} from './init-scripts.js';
import {STARTED_AT} from './started-at.js';

export interface FakeHostWindow extends Window {
  fakeHost: {
    readonly newRun: FakeNewRun;
    listing(listing: unknown): void;
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
    initScript(installFakeBridge, seed),
    initScript(installFakeMachines, seed),
    initScript(installFakeMachineSettings, seed),
    initScript(installFakeNewRun, seed),
    initScript(installFakeProjectRuns, seed),
    initScript(installFakeExampleDestination, seed),
  ]);
  await page.goto('./shell.html');
  await expect(page.getByRole('navigation', {name: 'VibeSys'})).toBeVisible();
}

/** Opens the app and goes to New run by the rail, as a user starts a run. */
export async function openNewRun(page: Page, seed: FakeBridgeSeed = SEED): Promise<void> {
  // The run that Start run opens is read over the gateway like every run.
  await installGateway(page, wire(storeRunFixture('live')));
  await open(page, seed);
  await page
    .getByRole('navigation', {name: 'VibeSys'})
    .getByRole('button', {name: 'New', exact: true})
    .click();
  await expect(page.getByRole('main')).toBeVisible();
}

/** The rail: the app's one navigation. */
export const sidebar = (page: Page) => page.getByRole('navigation', {name: 'VibeSys'});

/**
 * What the page draws inside the window controls' rectangle (`--chrome-inset-width` by
 * `--chrome-inset-height`, from the window's top-left corner), other than the sidebar toggle that
 * is meant to sit beside them: text and controls, as `text at x,y`. Empty when nothing is covered.
 */
export async function drawnUnderWindowControls(page: Page): Promise<string[]> {
  return page.evaluate(() => {
    const style = getComputedStyle(document.documentElement);
    const inset = {
      width: Number.parseFloat(style.getPropertyValue('--chrome-inset-width')),
      height: Number.parseFloat(style.getPropertyValue('--chrome-inset-height')),
    };
    const drawn = [...document.querySelectorAll('body *')].filter(
      element =>
        element.matches('a, button, input, select, textarea, [tabindex]:not([tabindex="-1"])') ||
        [...element.childNodes].some(
          node => node.nodeType === Node.TEXT_NODE && node.textContent?.trim() !== '',
        ),
    );
    return drawn.flatMap(element => {
      const box = element.getBoundingClientRect();
      const hit = box.width > 0 && box.left < inset.width && box.top < inset.height;
      return hit ? [`${element.textContent?.trim()} at ${box.left},${box.top}`] : [];
    });
  });
}
