import AxeBuilder from '@axe-core/playwright';
import {expect, type Locator, type Page, test} from '@playwright/test';
import {type FakeBridgeSeed, installFakeBridge, installFakeMachines} from './fake-bridge.js';
import {lineageRun} from './fake-lineage.js';
import {installFakeRun} from './fake-run.js';
import {installFakeServer} from './fake-server.js';

/**
 * The Ideas pane as a lineage tree against the Fake bridge: a small live-like run (10 ideas) and
 * one of weeks (600 ideas over 14 days). The best idea's chain is open with the accent rail and its
 * `best` badge, every other branch is one folded row that opens in place, the list scrolls windowed,
 * and the header line replaces the old best-path row. The performance chart joins no dots (only
 * its step line is drawn), and pointing at an idea rings the dots of its chain there and lights
 * it in the tree. `LEVEL_SHOTS` names a directory to save the app window in, at 1440
 * and at 1000 px wide.
 */

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

const shots = process.env['LEVEL_SHOTS'];

async function openRun(
  page: Page,
  options: {readonly count: number; readonly chain: number; readonly days: number},
): Promise<void> {
  await page.addInitScript(installFakeBridge, SEED);
  await page.addInitScript(installFakeMachines, SEED);
  await page.addInitScript(installFakeRun, {streams: [], text: {}});
  await page.addInitScript(installFakeServer, lineageRun(RUN, options));
  await page.goto('./shell.html');
  await page
    .getByRole('navigation', {name: 'VibeSys'})
    .getByRole('button', {name: /^\S+ queue-rs · mpmc/})
    .click();
}

/** The pane's counts stay on one line, and no row repeats its title as its subtitle. */
async function expectTidy(page: Page): Promise<void> {
  const tops = await page
    .locator('.vs-dotlegend-item')
    .evaluateAll(items => items.map(item => Math.round(item.getBoundingClientRect().top)));
  expect(new Set(tops).size).toBe(1);
  const rows = await page
    .getByRole('listbox', {name: 'Ideas'})
    .getByRole('option')
    .evaluateAll(options =>
      options.map(option =>
        [...option.querySelectorAll('.vs-pickrow-text > .vs-text')].map(t => t.textContent ?? ''),
      ),
    );
  for (const [title = '', subtitle] of rows) {
    if (subtitle === undefined) continue;
    expect(subtitle.toLowerCase().replace(/^try:\s*/, '')).not.toBe(title.toLowerCase());
  }
}

async function shot(page: Page, name: string): Promise<void> {
  await expectTidy(page);
  if (shots !== undefined) await page.screenshot({path: `${shots}/${name}.png`});
}

/** The chart draws one path (the best-so-far step line) and no line from one dot to another. */
async function expectNoConnectors(chart: Locator): Promise<void> {
  await expect(chart.locator('.vs-chart-edge, .vs-chart-edges')).toHaveCount(0);
  await expect(chart.locator('path')).toHaveCount(1);
  await expect(chart.locator('path.vs-chart-step')).toHaveCount(1);
}

test.use({colorScheme: 'dark', viewport: {width: 1440, height: 900}});

test('a small run opens as a tree: the best chain open, other branches folded, one row to open each', async ({
  page,
}) => {
  await openRun(page, {count: 10, chain: 4, days: 1});
  const ideas = page.getByRole('listbox', {name: 'Ideas'});
  await expect(ideas.getByRole('option').first()).toBeVisible();
  await expect(page.getByRole('radio', {name: 'Tree'})).toBeChecked();
  await expect(page.getByText('best path')).toHaveCount(0);
  await expect(page.getByText(/^best:$/)).toBeVisible();
  await expect(page.getByText(/steps from input$/)).toBeVisible();
  expect((await new AxeBuilder({page}).analyze()).violations).toEqual([]);
  await shot(page, 'ideas-tree-10-1440');
  await page.setViewportSize({width: 1920, height: 1080});
  await shot(page, 'ideas-tree-10-1920');
  await page.setViewportSize({width: 1440, height: 900});

  // The chart joins no dots (only the best-so-far step line is drawn); pointing at a row rings
  // the dots of its chain.
  const chart = page.getByRole('group', {name: /of each measured idea/});
  await expectNoConnectors(chart);
  await expect(chart.locator('.vs-chart-lit')).toHaveCount(0);
  await ideas.getByRole('option').nth(3).hover();
  expect(await chart.locator('.vs-chart-lit').count()).toBeGreaterThan(0);
  await expectNoConnectors(chart);
  await shot(page, 'ideas-tree-10-lit-1440');
  await page.mouse.move(0, 0);

  const folded = ideas.getByRole('option').filter({hasText: '→'}).first();
  if ((await folded.count()) > 0) {
    const before = await ideas.getByRole('option').count();
    await folded.locator('.vs-pickrow-fold').click();
    expect(await ideas.getByRole('option').count()).toBeGreaterThan(before);
  }
  await page.setViewportSize({width: 1000, height: 800});
  await shot(page, 'ideas-tree-10-1000');
  await page.getByRole('radio', {name: 'List'}).check({force: true});
  await expect(ideas.getByRole('option').first()).toBeVisible();
});

test('a run of weeks draws only the rows near the view, and the best line shows the best idea', async ({
  page,
}) => {
  await openRun(page, {count: 600, chain: 15, days: 14});
  const ideas = page.getByRole('listbox', {name: 'Ideas'});
  await expect(ideas.getByRole('option').first()).toBeVisible();
  await expect(page.getByRole('radio', {name: 'Tree'})).toBeChecked();
  expect(await ideas.getByRole('option').count()).toBeLessThan(80);
  await expect(ideas.getByRole('option').filter({hasText: '… '}).first()).toBeVisible();
  await shot(page, 'ideas-tree-600-1440');
  await page.setViewportSize({width: 1920, height: 1080});
  await shot(page, 'ideas-tree-600-1920');
  await page.setViewportSize({width: 1440, height: 900});
  const chart = page.getByRole('group', {name: /of each measured idea/});
  await expectNoConnectors(chart);
  await ideas.getByRole('option').nth(5).hover();
  expect(await chart.locator('.vs-chart-lit').count()).toBeGreaterThan(1);
  await shot(page, 'ideas-tree-600-lit-1440');
  await page.mouse.move(0, 0);

  await page.locator('button[title^="Show the best idea"]').click();
  await expect(ideas.getByRole('option', {selected: true})).toBeVisible();
  // Pinning the best keeps its older ancestors folded.
  await expect(ideas.getByRole('option').filter({hasText: /… \d+ earlier/})).toHaveCount(1);
  await expect(ideas.getByRole('option', {selected: true}).getByText('best')).toBeVisible();
  await page.setViewportSize({width: 1000, height: 800});
  await shot(page, 'ideas-tree-600-1000');

  // A search keeps the idea it finds and, open, the ideas it builds on: the best chain's tail.
  const best = (await page.locator('button[title^="Show the best idea"]').textContent()) ?? '';
  await page.getByRole('searchbox', {name: 'Search ideas'}).fill(best);
  await expect(ideas.getByRole('option', {name: new RegExp(best)}).first()).toBeVisible();
  expect(await ideas.getByRole('option').count()).toBeLessThan(80);
  expect((await new AxeBuilder({page}).analyze()).violations).toEqual([]);
});
