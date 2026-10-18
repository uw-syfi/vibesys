import {mkdirSync} from 'node:fs';
import {expect, type Page, test} from '@playwright/test';
import {screenCheck} from './checks/screen-check.js';
import {
  installFakeBridge,
  installFakeDigests,
  installFakeMachines,
  installFakeProjectRuns,
} from './fake-bridge.js';
import {SEED, sidebar} from './world.js';

/**
 * The All runs screen's journey against the Fake bridge: "Show all" lists every run of every
 * project on each connected machine, grouped by machine, newest first, each row the sidebar's own;
 * a machine that cannot list a project says so and Reload asks again; a row opens its run.
 * `ALL_RUNS_DIR` names a directory for the window's screenshot.
 */

const shots = process.env['ALL_RUNS_DIR'] || undefined;
if (shots !== undefined) mkdirSync(shots, {recursive: true});

const NOW = Date.parse('2026-10-17T12:00:00Z');
const HOUR = 3_600_000;
const QUEUE = '/home/me/projects/queue-rs';
const STORE = '/home/me/projects/store';

const ago = (hours: number): number => NOW - hours * HOUR;
const listing = (host: string) => ({
  kind: 'listed',
  host,
  live: [],
  ended: [],
  unreadable: [],
  unverified: [],
});
const digest = (hours: number, outcome: string, ratio: number | null) => ({
  kind: 'read',
  createdAt: ago(hours),
  ideas: 5,
  adoptedRatio: ratio,
  durationSeconds: 1800,
  outcome,
  closing: outcome === 'failed' ? 'failed' : 'finished',
  over: true,
});

const WORLD = {
  ...SEED,
  listings: [listing('ssh:host-a'), listing('ssh:host-b')],
  recent: [
    ['ssh:host-a', QUEUE, 'a-new', 'mpmc'],
    ['ssh:host-a', STORE, 'a-lookup', 'lookup'],
    ['ssh:host-b', QUEUE, 'b-solo', 'mpmc'],
  ].map(([host, project, runId, task], index) => ({
    host,
    hostLabel: host,
    project,
    task,
    runId,
    instanceId: null,
    attachedAt: 10 - index,
  })),
  projectRuns: {
    [`ssh:host-a|${QUEUE}`]: [
      {runId: 'a-old', task: 'mpmc', createdAt: ago(30)},
      {runId: 'a-new', task: 'mpmc', createdAt: ago(2)},
      {runId: 'a-mid', task: null, createdAt: ago(9)},
    ],
    [`ssh:host-a|${STORE}`]: [{runId: 'a-lookup', task: 'lookup', createdAt: ago(5)}],
    [`ssh:host-b|${QUEUE}`]: [{runId: 'b-solo', task: 'mpmc', createdAt: ago(12)}],
  },
  digests: {
    'a-new': digest(2, 'adopted', 1.5),
    'a-mid': digest(9, 'failed', null),
    'a-old': digest(30, 'stopped', 1.1),
    'a-lookup': digest(5, 'adopted', 2.25),
    'b-solo': digest(12, 'adopted', 1.9),
  },
};

async function open(page: Page, world: typeof WORLD = WORLD): Promise<void> {
  await page.clock.setFixedTime(NOW);
  await page.addInitScript(installFakeBridge, world);
  await page.addInitScript(installFakeMachines, world);
  await page.addInitScript(installFakeDigests, world);
  await page.addInitScript(installFakeProjectRuns, world);
  await page.goto('./shell.html');
  await expect(sidebar(page)).toBeVisible();
  await sidebar(page)
    .getByRole('button', {name: /^Show all/})
    .click();
  await expect(page.getByRole('heading', {name: 'All runs'})).toBeVisible();
}

const screen = (page: Page) => page.locator('main');
const rowsOf = (page: Page, machine: string) =>
  screen(page).locator('.vs-sidebar-group', {hasText: machine}).locator('.vs-sidebar-item');

test('lists every run of every project, by machine, newest first', async ({page}) => {
  await open(page);

  const headers = await screen(page).locator('.vs-sidebar-group-title').allInnerTexts();
  expect(headers.map(text => text.replace(/\s+/g, ' ').trim())).toEqual(['host-a 4', 'host-b 1']);
  const titles = async (machine: string) =>
    (await rowsOf(page, machine).allInnerTexts()).map(text => text.replace(/\s+/g, ' ').trim());
  const [first, second, third, fourth] = await titles('host-a');
  expect(first).toMatch(/^queue-rs · mpmc .*1\.50×/);
  expect(second).toMatch(/^store · lookup .*2\.25×/);
  expect(third).toMatch(/^queue-rs (?!·).*failed/);
  expect(fourth).toMatch(/^queue-rs · mpmc .*stopped/);
  expect(await titles('host-b')).toHaveLength(1);
  expect(await screenCheck(page)).toEqual([]);
  if (shots !== undefined) await page.screenshot({path: `${shots}/all-runs.png`});
});

test('a machine that cannot list a project says so, and Reload asks again', async ({page}) => {
  const {[`ssh:host-b|${QUEUE}`]: _lost, ...rest} = WORLD.projectRuns;
  await open(page, {...WORLD, projectRuns: rest} as typeof WORLD);

  await expect(screen(page).getByText('Some runs could not be listed.')).toBeVisible();
  await expect(rowsOf(page, 'host-a')).toHaveCount(4);

  await page.getByRole('button', {name: 'Reload'}).click();
  await expect(screen(page).getByText('Some runs could not be listed.')).toBeVisible();
  await expect(rowsOf(page, 'host-a')).toHaveCount(4);
});

test('a row opens its run, as a Recent row does', async ({page}) => {
  await open(page);

  await rowsOf(page, 'host-b').first().click();

  await expect(page.getByRole('heading', {name: 'All runs'})).toHaveCount(0);
  await expect(sidebar(page).locator('.vs-sidebar-item[data-active]')).toContainText('queue-rs');
});
