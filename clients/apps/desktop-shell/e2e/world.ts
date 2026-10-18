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
    toggleSidebar(): void;
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
      harness_cli_names: {claude: 'Claude Code', codex: 'Codex'},
      runs: {live: 1, ended_today: 2},
    },
    harnesses: [
      {
        provider: 'claude',
        model: 'claude-opus-4-5',
        default: true,
        display: {
          providerName: 'Claude Code',
          modelName: 'Opus 4.5',
          family: 'Claude',
          tier: 3,
          tierName: 'Opus',
        },
      },
      {
        provider: 'claude',
        model: 'claude-sonnet-4-5',
        default: false,
        display: {
          providerName: 'Claude Code',
          modelName: 'Sonnet 4.5',
          family: 'Claude',
          tier: 2,
          tierName: 'Sonnet',
        },
      },
      {
        provider: 'codex',
        model: 'gpt-5-codex',
        default: true,
        display: {providerName: 'Codex', modelName: 'GPT-5 Codex'},
      },
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
