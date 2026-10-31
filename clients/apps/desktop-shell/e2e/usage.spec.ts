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
import {addInitScripts, initScript} from './init-scripts.js';
import {nativeFixtureScript} from './native-fixture.js';
import {runFixtureFor, seedFor, USAGE_CASES, type UsageCase} from './usage-world.js';

/**
 * The Usage page, reached by the rail over a Fake of the bridge, at the three widths the shell is
 * used at: empty window, one run, many runs, turns without a price, a helper that predates the
 * question and one that fails. One session per case is resized across the widths. Each screen
 * passes the layout and wording checks, does not scroll as a page, and says only labels, figures
 * and status words. `USAGE_SCREENS_DIR=<dir>` writes each screen's screenshot (the page only).
 */

const WIDTHS = [1440, 1000, 660] as const;
const SHOTS = process.env['USAGE_SCREENS_DIR'] || undefined;
const DAY = 24 * 3_600_000;

interface UsageHost extends Window {
  fakeHost: {usageAsked: {since: number; until: number; runIds: string[]}[]};
}

async function open(page: Page, usage: UsageCase, width: number): Promise<void> {
  const seed = seedFor(usage);
  await page.setViewportSize({width, height: 900});
  await addInitScripts(page, [
    initScript(installFakeBridge, seed),
    initScript(installFakeProjectRuns, seed),
    initScript(installFakeNewRun, seed),
    initScript(installFakeUsage, seed),
    nativeFixtureScript(runFixtureFor(usage)),
  ]);
  await page.goto('./shell.html');
  await page
    .getByRole('navigation', {name: 'VibeSys'})
    .getByRole('button', {name: 'Usage'})
    .click();
  await expect(page.getByRole('heading', {name: 'Usage', level: 1})).toBeVisible();
  // The page has read its figures once it is no longer loading.
  await expect(page.getByText('Loading')).toHaveCount(0);
  await settled(page);
}

for (const usage of USAGE_CASES) {
  test(`the Usage page passes the screen checks at every width: ${usage.name}`, async ({page}) => {
    await open(page, usage, WIDTHS[0]);
    for (const width of WIDTHS) {
      await page.setViewportSize({width, height: 900});
      expect(await page.evaluate(() => window.innerWidth)).toBe(width);
      await settled(page);
      const name = `${usage.name}/${width}`;
      const result = report(`usage/${name}`, await screenCheck(page), KNOWN_FAILURES);
      expect(
        result.unexplained.map(each => `[${each.check}] ${each.message} (${each.where})`),
        name,
      ).toEqual([]);
      // The page never scrolls sideways: a table that is too wide scrolls in its own box.
      const overflow = await page.evaluate(
        () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
      );
      expect(overflow, name).toBeLessThanOrEqual(0);
      if (SHOTS !== undefined) {
        await page.getByRole('main').screenshot({path: `${SHOTS}/${usage.name}-${width}.png`});
      }
    }
  });
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

test('a figure stays on one line at 1440 however large it is', async ({page}) => {
  const large = USAGE_CASES.find(each => each.name === 'large-figures');
  if (large === undefined) throw new Error('the cases changed');
  await open(page, large, 1440);
  const lines = await page.evaluate(() =>
    [...document.querySelectorAll('.vs-tile-figure')].map(figure => {
      const walk = document.createTreeWalker(figure, NodeFilter.SHOW_TEXT);
      const tops = new Set<number>();
      for (let node = walk.nextNode(); node !== null; node = walk.nextNode()) {
        const range = document.createRange();
        range.selectNodeContents(node);
        for (const rect of range.getClientRects()) tops.add(Math.round(rect.top));
      }
      return {text: figure.textContent, tops: tops.size};
    }),
  );
  expect(lines.length).toBe(4);
  expect(lines.filter(line => line.tops !== 1)).toEqual([]);
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
  // Each count is understood from structure: the rows that hold it say so, and the chip lists them.
  await expect(page.getByText('3 unpriced', {exact: true}).first()).toBeVisible();
  await expect(page.getByText('2 unpriced', {exact: true}).first()).toBeVisible();
  const chip = page.locator('span[title]').filter({hasText: '5 unpriced'});
  expect(await chip.getAttribute('title')).toMatch(/: 3\n.*: 2/);
  const missing = page.locator('span[title]').filter({hasText: '1 run not counted'});
  expect(await missing.getAttribute('title')).toContain('20261001-000001');
});

test('an unpriced count the helper cannot itemise says so in its pointer', async ({page}) => {
  const old = USAGE_CASES.find(each => each.name === 'unpriced-no-breakdown');
  if (old === undefined) throw new Error('the cases changed');
  await open(page, old, 1440);
  await expect(page.getByText('2 unpriced', {exact: true})).toBeVisible();
  const chip = page.locator('span[title]').filter({hasText: '2 unpriced'});
  // No row holds the count, so the pointer names the machine and what it cannot say.
  expect(await chip.getAttribute('title')).toMatch(/host-b: no per-run counts/);
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
});

/** The run the shell shows, from the window's address: its Usage face ends `&mode=usage`. */
const faceOf = (page: Page) => page.evaluate(() => window.location.hash);

test('a run opens on its Usage face from its row', async ({page}) => {
  const many = USAGE_CASES.find(each => each.name === 'many-runs');
  if (many === undefined) throw new Error('the cases changed');
  await open(page, many, 1440);
  const row = page.getByRole('table', {name: 'Cost by run'}).getByRole('row').nth(1);
  await row.click();
  await expect(page.getByRole('heading', {name: 'Usage', level: 1})).toHaveCount(0);
  expect(await faceOf(page)).toMatch(/^#run=[^&]+&mode=usage$/);
  // The run screen names the run opened: the first the world lists.
  await expect(page.getByRole('main')).toContainText(/host-b · 2026101\d-\d{6}/);
});

test('one Back leaves a run opened from its row for Usage', async ({page}) => {
  const many = USAGE_CASES.find(each => each.name === 'many-runs');
  if (many === undefined) throw new Error('the cases changed');
  await open(page, many, 1440);
  await page.getByRole('table', {name: 'Cost by run'}).getByRole('row').nth(1).click();
  await expect(page.getByRole('heading', {name: 'Usage', level: 1})).toHaveCount(0);
  await page.goBack();
  await expect(page.getByRole('heading', {name: 'Usage', level: 1})).toBeVisible();
  expect(await faceOf(page)).toBe('#usage');
});

test('a run row is titled as the run is everywhere, by its project, and never by its id', async ({
  page,
}) => {
  const many = USAGE_CASES.find(each => each.name === 'many-runs');
  if (many === undefined) throw new Error('the cases changed');
  await open(page, many, 1440);
  const first = page.getByRole('table', {name: 'Cost by run'}).getByRole('row').nth(1);
  await expect(first).toContainText('queue-rs');
  await expect(first).not.toContainText(/2026101\d-\d{6}/);
  // The task the machine lists is not a title: no row is named after it.
  await expect(first).not.toContainText('mpmc');
});

test('the rows open with the keyboard too', async ({page}) => {
  const one = USAGE_CASES.find(each => each.name === 'one-run');
  if (one === undefined) throw new Error('the cases changed');
  await open(page, one, 1000);
  const row = page.getByRole('table', {name: 'Cost by run'}).getByRole('row').nth(1);
  await row.focus();
  await page.keyboard.press('Enter');
  await expect(page.getByRole('heading', {name: 'Usage', level: 1})).toHaveCount(0);
  expect(await faceOf(page)).toMatch(/^#run=[^&]+&mode=usage$/);
});

test('Usage asks nothing until it is shown, and again each time it is shown', async ({page}) => {
  const one = USAGE_CASES.find(each => each.name === 'one-run');
  if (one === undefined) throw new Error('the cases changed');
  const asked = () =>
    page.evaluate(() => (window as unknown as UsageHost).fakeHost.usageAsked.length);
  const seed = seedFor(one);
  await addInitScripts(page, [
    initScript(installFakeBridge, seed),
    initScript(installFakeProjectRuns, seed),
    initScript(installFakeNewRun, seed),
    initScript(installFakeUsage, seed),
  ]);
  await page.goto('./shell.html');
  const rail = page.getByRole('navigation', {name: 'VibeSys'});
  await expect(page.getByRole('heading', {name: 'Runs', level: 1})).toBeVisible();
  expect(await asked()).toBe(0);
  await rail.getByRole('button', {name: 'Usage'}).click();
  await expect(page.getByText('Loading')).toHaveCount(0);
  const first = await asked();
  expect(first).toBeGreaterThan(0);
  await rail.getByRole('button', {name: 'Runs'}).click();
  await expect(page.getByRole('heading', {name: 'Runs', level: 1})).toBeVisible();
  await rail.getByRole('button', {name: 'Usage'}).click();
  await expect(page.getByText('Loading')).toHaveCount(0);
  expect(await asked()).toBeGreaterThan(first);
});

test('the Usage page has no accessibility violations', async ({page}) => {
  const many = USAGE_CASES.find(each => each.name === 'many-runs');
  if (many === undefined) throw new Error('the cases changed');
  await open(page, many, 1440);
  const result = await new AxeBuilder({page}).analyze();
  expect(result.violations.map(each => `${each.id}: ${each.help}`)).toEqual([]);
});
