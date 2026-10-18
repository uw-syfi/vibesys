import AxeBuilder from '@axe-core/playwright';
import {expect, type Page, test} from '@playwright/test';
import {type FakeBridgeSeed, installFakeBridge, installFakeMachines} from './fake-bridge.js';
import {lineageRun} from './fake-lineage.js';
import {installFakeRun} from './fake-run.js';
import {installFakeServer} from './fake-server.js';

const PROJECT = '/home/me/vibesys-projects/queue-rs';
const INSTANCE = '00000000000a';
const RUN = '20261017-120000';
const SEED: FakeBridgeSeed = {
  theme: 'ink',
  chromeInset: {width: 84, height: 40},
  appearance: {mode: 'dark', light: 'paper', dark: 'ink'},
  listings: [
    {
      kind: 'listed',
      host: 'ssh:host-b',
      live: [
        {
          id: INSTANCE,
          projectRoot: PROJECT,
          runId: RUN,
          status: 'running',
          phase: 'running',
          paused: null,
          end: null,
          startedAt: 1,
          reason: null,
        },
      ],
      ended: [],
      unreadable: [],
      unverified: [],
    },
  ],
  recent: [
    {
      host: 'ssh:host-b',
      hostLabel: 'host-b',
      project: PROJECT,
      task: 'mpmc',
      runId: RUN,
      instanceId: INSTANCE,
      attachedAt: 3,
    },
  ],
};

async function openRun(page: Page, count: number): Promise<void> {
  await page.addInitScript(installFakeBridge, SEED);
  await page.addInitScript(installFakeMachines, SEED);
  await page.addInitScript(installFakeRun, {streams: [], text: {}});
  await page.addInitScript(
    installFakeServer,
    lineageRun(RUN, {count, chain: count > 100 ? 15 : 4, days: count > 100 ? 14 : 1}),
  );
  await page.goto('./shell.html');
  await page
    .getByRole('navigation', {name: 'VibeSys'})
    .getByRole('button', {name: /^\S+ queue-rs · mpmc/})
    .click();
}

test.use({colorScheme: 'dark', viewport: {width: 1440, height: 900}});

test('ideas stay flat, searchable, sortable and accessible at wide and narrow widths', async ({
  page,
}) => {
  await openRun(page, 10);
  const ideas = page.getByRole('listbox', {name: 'Ideas'});
  await expect(ideas.getByRole('option').first()).toBeVisible();
  await expect(ideas.locator('.vs-guide, .vs-pickrow-fold')).toHaveCount(0);
  await expect(page.getByRole('radiogroup', {name: 'Ideas layout'})).toHaveCount(0);
  await expect(page.getByRole('combobox', {name: 'Sort ideas'})).toHaveValue('newest');
  await page.getByRole('combobox', {name: 'Sort ideas'}).selectOption('oldest');
  await expect(ideas.getByRole('option').first()).toContainText('Preallocate the arena');
  await page.getByRole('searchbox', {name: 'Search ideas'}).fill('Batch the enqueue');
  await expect(ideas.getByRole('option')).toHaveCount(1);
  expect((await new AxeBuilder({page}).analyze()).violations).toEqual([]);
  await page.setViewportSize({width: 1000, height: 800});
  await expect(ideas.getByRole('option').first()).toBeVisible();
  expect((await new AxeBuilder({page}).analyze()).violations).toEqual([]);
  await ideas.getByRole('option').first().click();
  await expect(page.getByRole('navigation', {name: 'Zoom'})).toBeVisible();
  await expect(page.getByText(/vs base/)).toHaveCount(0);
  await expect(page.getByText(/^from$/)).toHaveCount(0);
});

test('six hundred ideas render a window of rows and remain keyboard reachable', async ({page}) => {
  await openRun(page, 600);
  const ideas = page.getByRole('listbox', {name: 'Ideas'});
  await expect(ideas.getByRole('option').first()).toBeVisible();
  expect(await ideas.getByRole('option').count()).toBeLessThan(80);
  await ideas.focus();
  await page.keyboard.press('End');
  await expect(ideas.getByRole('option').last()).toBeFocused();
  await page.keyboard.press('Enter');
  await expect(page.getByRole('navigation', {name: 'Zoom'})).toBeVisible();
});
