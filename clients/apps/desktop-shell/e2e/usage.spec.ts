import AxeBuilder from '@axe-core/playwright';
import {expect, type Page, test} from '@playwright/test';
import {KNOWN_FAILURES} from './checks/known-failures.js';
import {report, screenCheck} from './checks/screen-check.js';
import {settled} from './checks/settle.js';
import {
  installFakeBridge,
  installFakeNewRun,
  installFakeProjectRuns,
  installFakeUsage,
} from './fake-bridge.js';
import {seedFor, USAGE_CASES, USAGE_PROJECT, type UsageCase} from './usage-world.js';

/**
 * The Usage page over a Fake of the bridge, at the three widths the shell is used at: empty
 * window, one run, many runs, turns without a price, a helper that predates the question and one
 * that fails. Each screen passes the layout and wording checks, does not scroll as a page, and
 * says only labels, figures and status words. `USAGE_SCREENS_DIR=<dir>` writes each screen's
 * screenshot (the page only).
 */

const WIDTHS = [1440, 1000, 660] as const;
const SHOTS = process.env['USAGE_SCREENS_DIR'] || undefined;
const DAY = 24 * 3_600_000;

interface UsageHost extends Window {
  fakeHost: {usageAsked: {since: number; until: number; runIds: string[]}[]};
  openedRuns: {host: string; project: string; runId: string}[];
}

async function open(page: Page, usage: UsageCase, width: number): Promise<void> {
  const seed = seedFor(usage);
  await page.setViewportSize({width, height: 900});
  await page.addInitScript(installFakeBridge, seed);
  await page.addInitScript(installFakeProjectRuns, seed);
  await page.addInitScript(installFakeNewRun, seed);
  await page.addInitScript(installFakeUsage, seed);
  await page.goto('./usage.html');
  await expect(page.getByRole('heading', {name: 'Usage', level: 1})).toBeVisible();
  // The page has read its figures once it is no longer loading.
  await expect(page.getByText('Loading')).toHaveCount(0);
  await settled(page);
}

for (const usage of USAGE_CASES) {
  for (const width of WIDTHS) {
    const name = `${usage.name}/${width}`;
    test(`the Usage page passes the screen checks: ${name}`, async ({page}) => {
      await open(page, usage, width);
      expect(await page.evaluate(() => window.innerWidth)).toBe(width);
      const result = report(`usage/${name}`, await screenCheck(page), KNOWN_FAILURES);
      expect(
        result.unexplained.map(each => `[${each.check}] ${each.message} (${each.where})`),
      ).toEqual([]);
      // The page never scrolls sideways: a table that is too wide scrolls in its own box.
      const overflow = await page.evaluate(
        () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
      );
      expect(overflow).toBeLessThanOrEqual(0);
      if (SHOTS !== undefined) {
        await page.screenshot({path: `${SHOTS}/${usage.name}-${width}.png`});
      }
    });
  }
}

/** The text a person reads on the page, one string per element that holds text directly. */
async function words(page: Page): Promise<string[]> {
  return page.evaluate(() =>
    [...document.body.querySelectorAll('*')].flatMap(element =>
      [...element.childNodes]
        .filter(node => node.nodeType === Node.TEXT_NODE)
        .map(node => node.textContent?.trim() ?? '')
        .filter(text => text !== ''),
    ),
  );
}

for (const usage of USAGE_CASES) {
  test(`the Usage page writes no sentence and no em dash: ${usage.name}`, async ({page}) => {
    await open(page, usage, 1440);
    const written = await words(page);
    expect(written.length).toBeGreaterThan(0);
    for (const text of written) {
      expect(text, text).not.toContain('—');
      expect(text, text).not.toMatch(/[.!?](\s|$)/);
    }
  });
}

test('a helper that predates the question reads one word and shows no figures', async ({page}) => {
  const unsupported = USAGE_CASES.find(each => each.name === 'unsupported-helper');
  if (unsupported === undefined) throw new Error('the cases changed');
  await open(page, unsupported, 1440);
  await expect(page.getByText('Unsupported', {exact: true})).toBeVisible();
  await expect(page.getByRole('table')).toHaveCount(0);
});

test('a failed question reads Unavailable and Retry asks again', async ({page}) => {
  const failed = USAGE_CASES.find(each => each.name === 'failed-question');
  if (failed === undefined) throw new Error('the cases changed');
  await open(page, failed, 1440);
  await expect(page.getByText('Unavailable', {exact: true})).toBeVisible();
  const before = await page.evaluate(
    () => (window as unknown as UsageHost).fakeHost.usageAsked.length,
  );
  await page.getByRole('button', {name: 'Retry'}).click();
  await expect
    .poll(() => page.evaluate(() => (window as unknown as UsageHost).fakeHost.usageAsked.length))
    .toBeGreaterThan(before);
  await expect(page.getByText('Unavailable', {exact: true})).toBeVisible();
});

test('the figures are the four tiles, the runs and the agents the helper summed', async ({
  page,
}) => {
  const many = USAGE_CASES.find(each => each.name === 'many-runs');
  if (many === undefined) throw new Error('the cases changed');
  await open(page, many, 1440);
  const tiles = page.getByRole('region', {name: 'Totals'});
  await expect(tiles).toContainText('Spent');
  await expect(tiles).toContainText('Per hour now');
  await expect(tiles).toContainText('$12.50/h');
  await expect(tiles).toContainText('Tokens');
  await expect(tiles).toContainText('50.2M');
  await expect(tiles).toContainText('Per new best');
  await expect(tiles).toContainText('7 new bests');
  const runs = page.getByRole('table', {name: 'Cost by run'});
  expect(await runs.getByRole('row').count()).toBe(41);
  // The costliest first, each with its cost.
  await expect(runs.getByRole('row').nth(1)).toContainText('$900.00');
  const agents = page.getByRole('table', {name: 'Cost by agent'});
  // Fourteen agents, of which twelve differ in role, harness or model: one row each, and the header.
  expect(await agents.getByRole('row').count()).toBe(13);
  // A model is named as the machine's catalogue names it; its id rests in the pointer.
  await expect(agents.getByText('Opus 4.5').first()).toBeVisible();
  await expect(agents.getByText('Claude Code').first()).toBeVisible();
  await expect(page.getByText('7 more', {exact: true})).toBeVisible();
});

test('what the helper could not price or place is shown as status words', async ({page}) => {
  const unpriced = USAGE_CASES.find(each => each.name === 'unpriced-turns');
  if (unpriced === undefined) throw new Error('the cases changed');
  await open(page, unpriced, 1440);
  for (const label of ['5 unpriced', '2 untimed', '1 run not counted']) {
    await expect(page.getByText(label, {exact: true})).toBeVisible();
  }
  await expect(page.getByRole('region', {name: 'Totals'})).toContainText('none');
});

test('the window select asks over that window, and a run opens from its row', async ({page}) => {
  const many = USAGE_CASES.find(each => each.name === 'many-runs');
  if (many === undefined) throw new Error('the cases changed');
  await open(page, many, 1440);
  const asked = () => page.evaluate(() => (window as unknown as UsageHost).fakeHost.usageAsked);
  const before = (await asked()).length;
  const first = (await asked()).at(-1);
  expect(first?.until).toBeGreaterThan(0);
  expect((first?.until ?? 0) - (first?.since ?? 0)).toBe(7 * DAY);
  // No more than the helper's bound of runs is named in one question.
  expect(first?.runIds.length).toBe(40);

  const select = page.getByLabel('Window');
  await select.selectOption({label: 'Two weeks'});
  await expect.poll(async () => (await asked()).length).toBe(before + 1);
  const fortnight = (await asked()).at(-1);
  expect((fortnight?.until ?? 0) - (fortnight?.since ?? 0)).toBe(14 * DAY);

  await select.selectOption({label: 'Today'});
  await expect.poll(async () => (await asked()).length).toBe(before + 2);
  const today = (await asked()).at(-1);
  const midnight = await page.evaluate(
    until => new Date(until).setHours(0, 0, 0, 0),
    today?.until ?? 0,
  );
  expect(today?.since).toBe(midnight);
  await expect(page.getByText('Loading')).toHaveCount(0);

  await page.getByRole('button', {name: 'Refresh'}).click();
  await expect.poll(async () => (await asked()).length).toBe(before + 3);
  await expect(page.getByText('Loading')).toHaveCount(0);

  const row = page.getByRole('table', {name: 'Cost by run'}).getByRole('row').nth(1);
  await row.click();
  const opened = await page.evaluate(() => (window as unknown as UsageHost).openedRuns);
  expect(opened).toHaveLength(1);
  expect(opened[0]).toMatchObject({host: USAGE_PROJECT.host, project: USAGE_PROJECT.project});
  // The top row is the first run listed: its id is the one opened.
  expect(opened[0]?.runId).toMatch(/^2026101\d-\d{6}$/);
});

test('the rows open with the keyboard too', async ({page}) => {
  const one = USAGE_CASES.find(each => each.name === 'one-run');
  if (one === undefined) throw new Error('the cases changed');
  await open(page, one, 1000);
  const row = page.getByRole('table', {name: 'Cost by run'}).getByRole('row').nth(1);
  await row.focus();
  await page.keyboard.press('Enter');
  const opened = await page.evaluate(() => (window as unknown as UsageHost).openedRuns);
  expect(opened).toHaveLength(1);
});

test('the Usage page has no accessibility violations', async ({page}) => {
  const many = USAGE_CASES.find(each => each.name === 'many-runs');
  if (many === undefined) throw new Error('the cases changed');
  await open(page, many, 1440);
  const result = await new AxeBuilder({page}).analyze();
  expect(result.violations.map(each => `${each.id}: ${each.help}`)).toEqual([]);
});
