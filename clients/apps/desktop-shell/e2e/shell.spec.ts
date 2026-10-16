import AxeBuilder from '@axe-core/playwright';
import {expect, type Page, test} from '@playwright/test';
import {type FakeBridgeSeed, installFakeBridge} from './fake-bridge.js';

interface FakeHostWindow extends Window {
  fakeHost: {listing(listing: unknown): void; recent(runs: readonly unknown[]): void};
}

const PROJECT = '/home/me/projects/queue-rs';

function server(id: string, runId: string, phase: string): Record<string, unknown> {
  const end =
    phase === 'completed' ? {outcome: 'adopted', reason: 'kept the best', resumable: false} : null;
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

function listed(live: unknown[], ended: unknown[]): Record<string, unknown> {
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

const SEED: FakeBridgeSeed = {
  theme: 'paper',
  appearance: {mode: 'system', light: 'paper', dark: 'ink'},
  listings: [
    listed(
      [server('00000000000a', '20261015-163047', 'running')],
      [server('00000000000b', '20261014-090000', 'failed')],
    ),
  ],
  recent: RECENT,
};

async function open(page: Page, seed: FakeBridgeSeed = SEED): Promise<void> {
  await page.addInitScript(installFakeBridge, seed);
  await page.goto('./shell.html');
  await expect(page.getByRole('navigation', {name: 'VibeSys'})).toBeVisible();
}

const sidebar = (page: Page) => page.getByRole('navigation', {name: 'VibeSys'});

test('the sidebar lists live and recent runs, and a run that ends moves to Recent', async ({
  page,
}) => {
  await open(page);
  const live = sidebar(page).getByRole('region', {name: 'Live'});
  const recent = sidebar(page).getByRole('region', {name: 'Recent'});
  await expect(live.getByRole('button', {name: /^\S+ queue-rs · mpmc/})).toBeVisible();
  await expect(recent.getByRole('button', {name: /^\S+ queue-rs · spsc/})).toContainText('failed');
  await expect(recent.getByRole('button', {name: /^\S+ queue-rs · mpmc/})).toHaveCount(0);

  await page.evaluate(
    ([listing]) => (window as unknown as FakeHostWindow).fakeHost.listing(listing),
    [
      listed(
        [],
        [
          server('00000000000a', '20261015-163047', 'completed'),
          server('00000000000b', '20261014-090000', 'failed'),
        ],
      ),
    ],
  );
  await expect(live).toHaveCount(0);
  await expect(recent.getByRole('button', {name: /^\S+ queue-rs · mpmc/})).toContainText(
    'finished',
  );
});

test('Archive hides a run from Recent', async ({page}) => {
  await open(page);
  const recent = sidebar(page).getByRole('region', {name: 'Recent'});
  await recent.getByRole('button', {name: 'More for queue-rs · spsc'}).click();
  await page.getByRole('menuitem', {name: 'Archive'}).click();
  await expect(recent.getByRole('button', {name: /^\S+ queue-rs · spsc/})).toHaveCount(0);
});

test('Settings changes the theme at once and keeps it', async ({page}) => {
  await open(page);
  await sidebar(page).getByRole('button', {name: 'Settings'}).click();
  await expect(page.getByRole('heading', {name: 'Appearance'})).toBeVisible();
  await page.getByRole('radio', {name: 'Always dark'}).check();
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'ink');
  await page.getByRole('button', {name: 'Midnight'}).click();
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'midnight');
  await expect(page.getByRole('button', {name: 'Midnight'})).toHaveAttribute(
    'aria-pressed',
    'true',
  );
  await page.getByRole('switch', {name: /Notify me/}).uncheck();
  await expect(page.getByRole('switch', {name: /Notify me/})).not.toBeChecked();
  const axe = await new AxeBuilder({page}).analyze();
  expect(axe.violations).toEqual([]);
});

test('Match system follows the system appearance', async ({page}) => {
  await page.emulateMedia({colorScheme: 'dark'});
  await open(page, {...SEED, theme: 'ink'});
  await sidebar(page).getByRole('button', {name: 'Settings'}).click();
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'ink');
  await page.emulateMedia({colorScheme: 'light'});
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'paper');
});
