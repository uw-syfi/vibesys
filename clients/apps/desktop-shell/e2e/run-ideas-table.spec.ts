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
  await expect(page.getByRole('button', {name: 'Close idea'})).toBeVisible();
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
  const scrollTop = await ideas.evaluate(list => list.scrollTop);
  await ideas.evaluate(list => list.setAttribute('data-test-persist', 'yes'));
  await page.keyboard.press('Enter');
  await expect(page.getByRole('button', {name: 'Close idea'})).toBeVisible();
  await expect(ideas).toHaveAttribute('data-test-persist', 'yes');
  expect(await ideas.evaluate(list => list.scrollTop)).toBe(scrollTop);
});

test('opening an idea preserves the Ideas list and controls at wide and narrow widths', async ({
  page,
}) => {
  await openRun(page, 10);
  const ideas = page.getByRole('listbox', {name: 'Ideas'});
  const controls = page.locator('.vs-pane', {has: ideas});
  const geometry = () =>
    ideas.evaluate(list => {
      const pane = list.closest('.vs-pane');
      const row = list.querySelector('.vs-pickrow');
      const figure = row?.querySelector('.vs-pickrow-figure');
      const bounds = (element: Element | null | undefined) => {
        const rect = element?.getBoundingClientRect();
        return rect === undefined ? null : [rect.x, rect.y, rect.width, rect.height];
      };
      return {
        pane: bounds(pane),
        list: bounds(list),
        row: bounds(row),
        figure: bounds(figure),
        scrollTop: list.scrollTop,
        text: row?.querySelector('.vs-pickrow-text')?.textContent,
      };
    });
  for (const width of [1440, 1000]) {
    await page.setViewportSize({width, height: 900});
    await expect(controls.locator('.vs-dotlegend-item')).toHaveCount(3);
    await expect(page.getByRole('searchbox', {name: 'Search ideas'})).toBeVisible();
    await expect(page.getByRole('combobox', {name: 'Sort ideas'})).toBeVisible();
    await ideas.evaluate(list => list.setAttribute('data-test-persist', 'yes'));
    const before = await geometry();
    if (process.env['RUN_SHOTS'] !== undefined) {
      await page.screenshot({path: `${process.env['RUN_SHOTS']}/ideas-closed-${width}.png`});
    }
    await ideas.getByRole('option').first().click();
    await expect(page.getByRole('button', {name: 'Close idea'})).toBeVisible();
    await expect(controls.locator('.vs-dotlegend-item')).toHaveCount(3);
    await expect(ideas).toHaveAttribute('data-test-persist', 'yes');
    await expect(page.getByRole('searchbox', {name: 'Search ideas'})).toBeVisible();
    await expect(page.getByRole('combobox', {name: 'Sort ideas'})).toBeVisible();
    if (process.env['RUN_SHOTS'] !== undefined) {
      await page.screenshot({path: `${process.env['RUN_SHOTS']}/ideas-open-${width}.png`});
    }
    expect(await geometry()).toEqual(before);
    await page.getByRole('button', {name: 'Close idea'}).click();
    await expect(ideas).toHaveAttribute('data-test-persist', 'yes');
    expect(await geometry()).toEqual(before);
  }
});
