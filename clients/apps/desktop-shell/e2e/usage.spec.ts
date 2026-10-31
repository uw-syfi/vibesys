import {mkdirSync, writeFileSync} from 'node:fs';
import AxeBuilder from '@axe-core/playwright';
import {expect, type Page, test} from '@playwright/test';
import {contactSheetHtml, type SheetEntry} from './checks/contact-sheet.js';
import {KnownLedger} from './checks/known-ledger.js';
import {SCREEN_CHECKS, screenCheck} from './checks/screen-check.js';
import {settled} from './checks/settle.js';
import {
  installFakeBridge,
  installFakeChrome,
  installFakeMachines,
  installFakeNewRun,
  installFakeProjectRuns,
  installFakeUsage,
  installFakeUsagePrices,
} from './fake-bridge.js';
import {addInitScripts, initScript} from './init-scripts.js';
import {nativeFixtureScript} from './native-fixture.js';
import {runFixtureFor, seedFor, USAGE_CASES, type UsageCase} from './usage-world.js';

/**
 * The Usage page, reached by the rail over a Fake of the bridge, at the three widths the shell is
 * used at: an empty period, one run, many runs, a person's own use beside the runs, models without
 * a price, a figure too large for its box, a host that predates the question and one that fails.
 * One session per case is resized across the widths. Each screen passes the layout and wording
 * checks, does not scroll as a page, and says only labels, figures and status words.
 * `USAGE_SCREENS_DIR=<dir>` also writes each screen's screenshot and the contact sheet (the page only).
 */

const WIDTHS = [1440, 1000, 660] as const;
const SHOTS = process.env['USAGE_SCREENS_DIR'] || undefined;
const ledger = new KnownLedger('usage');

interface Asked {
  readonly host: string;
  readonly edges: readonly number[];
}

interface UsageHost extends Window {
  fakeHost: {
    usageAsked: Asked[];
    usageRefreshed: unknown[];
    usageSaved: {host: string; baseRevision: number; overrides: {model: string}[]}[];
  };
}

const caseNamed = (name: string): UsageCase => {
  const found = USAGE_CASES.find(each => each.name === name);
  if (found === undefined) throw new Error(`the cases changed: ${name}`);
  return found;
};

async function open(page: Page, usage: UsageCase, width: number): Promise<void> {
  const seed = seedFor(usage);
  await page.setViewportSize({width, height: 900});
  await addInitScripts(page, [
    initScript(installFakeChrome, seed),
    initScript(installFakeBridge, seed),
    initScript(installFakeMachines, seed),
    initScript(installFakeProjectRuns, seed),
    initScript(installFakeNewRun, seed),
    initScript(installFakeUsagePrices, undefined),
    initScript(installFakeUsage, seed),
    nativeFixtureScript(runFixtureFor(usage)),
  ]);
  await page.goto('./shell.html');
  await page
    .getByRole('navigation', {name: 'VibeSys'})
    .getByRole('button', {name: 'Usage'})
    .click();
  await expect(page.getByRole('heading', {name: 'Usage', level: 1})).toBeVisible();
  // The page has read its figures once nothing is loading, or has said why it cannot.
  await idle(page);
  await settled(page);
}

/** The page has nothing in flight: no loading body and no header word for work in hand. */
async function idle(page: Page): Promise<void> {
  for (const word of ['Loading', 'Refreshing', 'Scanning', 'Saving']) {
    await expect(page.getByText(word, {exact: true})).toHaveCount(0);
  }
}

const asked = (page: Page) =>
  page.evaluate(() => (window as unknown as UsageHost).fakeHost.usageAsked);

/** The page's hero figure: the first figure under the title. */
const hero = (page: Page) => page.locator('#usage-hero');

for (const usage of USAGE_CASES) {
  test(`the Usage page passes the screen checks at every width: ${usage.name}`, async ({page}) => {
    await open(page, usage, WIDTHS[0]);
    for (const width of WIDTHS) {
      await page.setViewportSize({width, height: 900});
      expect(await page.evaluate(() => window.innerWidth)).toBe(width);
      await settled(page);
      const name = `${usage.name}/${width}`;
      expect(ledger.unexplained(`usage/${name}`, await screenCheck(page, SCREEN_CHECKS))).toEqual(
        [],
      );
      // The page never scrolls sideways: a table that is too wide scrolls in its own box.
      const overflow = await page.evaluate(
        () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
      );
      expect(overflow, name).toBeLessThanOrEqual(0);
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

ledger.requireEveryFailure(() => ledger.screens >= WIDTHS.length * USAGE_CASES.length);

test('a host that predates the question reads Update required and shows no figures', async ({
  page,
}) => {
  await open(page, caseNamed('unsupported-host'), 1440);
  await expect(page.getByText('Update required', {exact: true})).toBeVisible();
  await expect(page.getByRole('table')).toHaveCount(0);
});

test('a failed question reads Unavailable and Retry asks again', async ({page}) => {
  await open(page, caseNamed('failed-question'), 1440);
  // The body says it once; the header has no word for a failure.
  await expect(page.getByText('Unavailable', {exact: true})).toHaveCount(1);
  const before = (await asked(page)).length;
  await page.getByRole('button', {name: 'Retry'}).click();
  await expect.poll(async () => (await asked(page)).length).toBeGreaterThan(before);
  await expect(page.getByText('Unavailable', {exact: true})).toHaveCount(1);
});

test('an empty period says No usage, at the page’s left edge, and shows no table', async ({
  page,
}) => {
  await open(page, caseNamed('empty-period'), 1440);
  await expect(page.getByText('No usage', {exact: true})).toBeVisible();
  await expect(page.getByRole('table', {name: 'Models'})).toHaveCount(0);
  // The empty state says so once: no figure of zero and no count of sessions above it.
  await expect(page.locator('#usage-hero')).toHaveCount(0);
  // With no provider there is nothing to switch on or off: no bare header row.
  await expect(page.getByRole('table', {name: 'Providers'})).toHaveCount(0);
});

test('the page title is the largest text on it, however large the figures are', async ({page}) => {
  await open(page, caseNamed('large-figures'), 1440);
  const sizes = await page.evaluate(() => {
    const size = (element: Element) => Number.parseFloat(getComputedStyle(element).fontSize);
    const title = document.querySelector('h1');
    const others = [...document.querySelectorAll('main *')]
      .filter(
        element =>
          element !== title &&
          element.children.length === 0 &&
          (element.textContent ?? '').trim() !== '',
      )
      .map(element => ({size: size(element), text: (element.textContent ?? '').trim()}))
      .sort((a, b) => b.size - a.size);
    return {title: title === null ? 0 : size(title), largest: others[0]};
  });
  expect(sizes.largest?.text, 'the largest other text').toBeDefined();
  expect(sizes.title, sizes.largest?.text).toBeGreaterThan(sizes.largest?.size ?? 0);
});

test('the figures are the hero, the providers, the models, the hours and days, the runs and the agents', async ({
  page,
}) => {
  await open(page, caseNamed('many-runs'), 1440);
  // Seven days: the base cost of every series, each bucket ramped 1, 3, 5, 2, 4, 1, 3.
  await expect(hero(page)).toHaveText('$817.00');
  await expect(page.getByRole('table', {name: 'Providers'}).getByRole('row')).toHaveCount(3);
  await expect(page.getByRole('table', {name: 'Models'}).getByRole('row')).toHaveCount(4);
  await expect(page.getByRole('table', {name: 'Time'}).getByRole('row')).toHaveCount(8);
  const runs = page.getByRole('table', {name: 'Cost by run'});
  await expect(runs.getByRole('row')).toHaveCount(41);
  // The costliest first, each with its cost.
  await expect(runs.getByRole('row').nth(1)).toContainText('$900.00');
  await expect(page.getByRole('table', {name: 'Cost by agent'}).getByRole('row')).toHaveCount(7);
  await expect(page.getByText('Per new best', {exact: true})).toBeVisible();
  await expect(page.getByText('Per hour now', {exact: true})).toBeVisible();
});

test('every period asks the host in the buckets it names, in both directions', async ({page}) => {
  await open(page, caseNamed('one-run'), 1440);
  const select = page.getByLabel('Period');
  // The page opens on the past week: seven days are eight edges.
  expect((await asked(page)).at(-1)?.edges).toHaveLength(8);
  const order = [
    ['Past 24 hours', 25],
    ['Past 7 days', 8],
    ['Past 30 days', 31],
    ['Past 90 days', 91],
    ['Past 30 days', 31],
    ['Past 7 days', 8],
    ['Past 24 hours', 25],
  ] as const;
  for (const [label, edges] of order) {
    const before = (await asked(page)).length;
    await select.selectOption({label});
    await expect.poll(async () => (await asked(page)).length).toBeGreaterThan(before);
    const last = (await asked(page)).at(-1);
    expect(last?.edges, label).toHaveLength(edges);
    // Whole quarter hours, increasing: what a host accepts.
    for (const [at, edge] of (last?.edges ?? []).entries()) {
      expect(edge % 900_000, label).toBe(0);
      if (at > 0) expect(edge).toBeGreaterThan(last?.edges[at - 1] ?? 0);
    }
    await idle(page);
    await expect(page.getByRole('table', {name: 'Time'}).getByRole('row')).toHaveCount(edges);
  }
});

test('Cost and Tokens switch the figures both ways without asking the host again', async ({
  page,
}) => {
  await open(page, caseNamed('one-run'), 1440);
  const before = (await asked(page)).length;
  await expect(hero(page)).toHaveText('$817.00');
  await page.getByRole('radio', {name: 'Tokens'}).check({force: true});
  await expect(hero(page)).toHaveText('125.4M');
  await page.getByRole('radio', {name: 'Cost'}).check({force: true});
  await expect(hero(page)).toHaveText('$817.00');
  expect((await asked(page)).length).toBe(before);
});

test('the source select and a provider toggle cut the figures by exactly the rows they leave out', async ({
  page,
}) => {
  await open(page, caseNamed('outside-runs'), 1440);
  const total = async () => (await hero(page).textContent()) ?? '';
  const dollars = async () => Number((await total()).replace(/[$,]/g, ''));
  const all = await dollars();
  const source = page.getByLabel('Source');
  await source.selectOption({label: 'VibeSys runs'});
  const runsOnly = await dollars();
  await source.selectOption({label: 'Outside runs'});
  const outside = await dollars();
  expect(runsOnly + outside).toBeCloseTo(all, 2);
  expect(outside).toBeGreaterThan(0);
  await source.selectOption({label: 'All sources'});
  expect(await dollars()).toBeCloseTo(all, 2);
  // A provider turned off takes exactly its rows out, and back on puts them back.
  await page.getByRole('checkbox', {name: 'Codex'}).uncheck();
  const withoutCodex = await dollars();
  expect(withoutCodex).toBeCloseTo(all - 5 * 19, 2);
  await page.getByRole('checkbox', {name: 'Codex'}).check();
  expect(await dollars()).toBeCloseTo(all, 2);
});

test('what the host could not price is a chip, and the rates it used are named', async ({page}) => {
  await open(page, caseNamed('unpriced-models'), 1440);
  await expect(page.getByText(/% unpriced$/)).toBeVisible();
  // The price list is dated, so a person sees how old the prices are, not only that they are bundled.
  await expect(page.getByText('Prices of 2026-10-30', {exact: true})).toBeVisible();
});

test('a model opens its detail, and its price is set, saved and read back', async ({page}) => {
  await open(page, caseNamed('one-run'), 1440);
  const models = page.getByRole('table', {name: 'Models'});
  await models.getByRole('row').nth(1).click();
  const detail = page.getByRole('dialog');
  await expect(detail).toBeVisible();
  await detail.getByRole('button', {name: 'Close'}).click();
  await expect(detail).toHaveCount(0);

  // The Price button sets the price and does not also open the detail.
  await models.getByRole('button', {name: 'Price'}).first().click();
  const prices = page.getByRole('dialog');
  await expect(prices).toBeVisible();
  await prices.getByRole('checkbox', {name: 'Automatic'}).uncheck();
  await prices.getByLabel('Input per 1M tokens').fill('7');
  await prices.getByRole('button', {name: 'Save'}).click();
  await expect(prices.getByText('Saved', {exact: true})).toBeVisible();
  const saved = await page.evaluate(() => (window as unknown as UsageHost).fakeHost.usageSaved);
  expect(saved).toHaveLength(1);
  expect(saved[0]?.baseRevision).toBe(0);
  expect(saved[0]?.overrides).toHaveLength(1);
  // The figures are read again, and the row says the price is set.
  await prices.getByRole('button', {name: 'Close'}).click();
  await expect(models.getByText('Set', {exact: true})).toBeVisible();
});

test('a price that cannot be saved says which field, and Save waits', async ({page}) => {
  await open(page, caseNamed('one-run'), 1440);
  await page
    .getByRole('table', {name: 'Models'})
    .getByRole('button', {name: 'Price'})
    .first()
    .click();
  const prices = page.getByRole('dialog');
  await prices.getByRole('checkbox', {name: 'Automatic'}).uncheck();
  await prices.getByLabel('Output per 1M tokens').fill('-1');
  await expect(prices.getByText('Invalid', {exact: true})).toBeVisible();
  await expect(prices.getByRole('button', {name: 'Save'})).toHaveAttribute('aria-disabled', 'true');
  expect(await page.evaluate(() => (window as unknown as UsageHost).fakeHost.usageSaved)).toEqual(
    [],
  );
});

/** The run the shell shows, from the window's address: its Usage face ends `&mode=usage`. */
const faceOf = (page: Page) => page.evaluate(() => window.location.hash);

test('a run opens on its Usage face from its row, and one Back returns to Usage', async ({
  page,
}) => {
  await open(page, caseNamed('many-runs'), 1440);
  const row = page.getByRole('table', {name: 'Cost by run'}).getByRole('row').nth(1);
  await row.click();
  await expect(page.getByRole('heading', {name: 'Usage', level: 1})).toHaveCount(0);
  expect(await faceOf(page)).toMatch(/^#run=[^&]+&mode=usage$/);
  await page.goBack();
  await expect(page.getByRole('heading', {name: 'Usage', level: 1})).toBeVisible();
  expect(await faceOf(page)).toBe('#usage');
});

test('a run row is titled as the run is everywhere, by its project, and never by its id', async ({
  page,
}) => {
  await open(page, caseNamed('many-runs'), 1440);
  const first = page.getByRole('table', {name: 'Cost by run'}).getByRole('row').nth(1);
  await expect(first).toContainText('queue-rs');
  await expect(first).not.toContainText(/2026101\d-\d{6}/);
});

test('the run rows open with the keyboard too', async ({page}) => {
  await open(page, caseNamed('one-run'), 1000);
  const row = page.getByRole('table', {name: 'Cost by run'}).getByRole('row').nth(1);
  await row.focus();
  await page.keyboard.press('Enter');
  await expect(page.getByRole('heading', {name: 'Usage', level: 1})).toHaveCount(0);
  expect(await faceOf(page)).toMatch(/^#run=[^&]+&mode=usage$/);
});

test('Refresh asks the host again', async ({page}) => {
  await open(page, caseNamed('one-run'), 1440);
  const before = (await asked(page)).length;
  await page.getByRole('button', {name: 'Refresh'}).click();
  await expect.poll(async () => (await asked(page)).length).toBeGreaterThan(before);
  await idle(page);
});

test('Usage asks nothing until it is shown, and again each time it is shown', async ({page}) => {
  const seed = seedFor(caseNamed('one-run'));
  await addInitScripts(page, [
    initScript(installFakeChrome, seed),
    initScript(installFakeBridge, seed),
    initScript(installFakeProjectRuns, seed),
    initScript(installFakeNewRun, seed),
    initScript(installFakeUsagePrices, undefined),
    initScript(installFakeUsage, seed),
  ]);
  await page.goto('./shell.html');
  const rail = page.getByRole('navigation', {name: 'VibeSys'});
  await expect(page.getByRole('heading', {name: 'Runs', level: 1})).toBeVisible();
  expect((await asked(page)).length).toBe(0);
  await rail.getByRole('button', {name: 'Usage'}).click();
  await expect(hero(page)).toHaveText('$817.00');
  const first = (await asked(page)).length;
  expect(first).toBeGreaterThan(0);
  await rail.getByRole('button', {name: 'Runs'}).click();
  await expect(page.getByRole('heading', {name: 'Runs', level: 1})).toBeVisible();
  await rail.getByRole('button', {name: 'Usage'}).click();
  await expect(hero(page)).toHaveText('$817.00');
  expect((await asked(page)).length).toBeGreaterThan(first);
});

test('the Usage page has no accessibility violations', async ({page}) => {
  await open(page, caseNamed('many-runs'), 1440);
  const result = await new AxeBuilder({page}).analyze();
  expect(result.violations.map(each => `${each.id}: ${each.help}`)).toEqual([]);
});

/**
 * Every control walked in both directions with a screenshot of each state, then the contact sheet
 * of them all. Only when `USAGE_SCREENS_DIR` is set; the shots are the page, never the desktop.
 */
test('every tab, period, source and dialog, walked and captured', async ({page, browser}) => {
  test.skip(SHOTS === undefined, 'set USAGE_SCREENS_DIR to write the screenshots and the sheet');
  const dir = SHOTS ?? '';
  mkdirSync(dir, {recursive: true});
  const entries: SheetEntry[] = [];
  const shoot = async (name: string): Promise<void> => {
    await settled(page);
    const file = `${name.replace(/[^a-z0-9]+/gi, '-')}.png`;
    await page.screenshot({path: `${dir}/${file}`});
    entries.push({name, file});
  };
  // Every case at every width, then one case walked through every control.
  for (const usage of USAGE_CASES) {
    const own = await page.context().newPage();
    await open(own, usage, WIDTHS[0]);
    for (const width of WIDTHS) {
      await own.setViewportSize({width, height: 900});
      await settled(own);
      const file = `case-${usage.name}-${width}.png`;
      await own.screenshot({path: `${dir}/${file}`});
      entries.push({name: `${usage.name} at ${width}`, file});
    }
    await own.close();
  }
  await open(page, caseNamed('outside-runs'), 1440);
  await shoot('seven days, cost');
  const periods = [
    'Past 24 hours',
    'Past 7 days',
    'Past 30 days',
    'Past 90 days',
    'Past 30 days',
    'Past 7 days',
    'Past 24 hours',
  ];
  for (const [at, label] of periods.entries()) {
    await page.getByLabel('Period').selectOption({label});
    await idle(page);
    await shoot(`period ${at + 1}, ${label}`);
  }
  await page.getByLabel('Period').selectOption({label: 'Past 7 days'});
  await idle(page);
  for (const tab of ['Tokens', 'Cost', 'Tokens', 'Cost']) {
    await page.getByRole('radio', {name: tab}).check({force: true});
    await shoot(`tab ${tab}`);
  }
  for (const source of ['VibeSys runs', 'Outside runs', 'All sources']) {
    await page.getByLabel('Source').selectOption({label: source});
    await shoot(`source ${source}`);
  }
  await page.getByRole('checkbox', {name: 'Codex'}).uncheck();
  await shoot('Codex off');
  await page.getByRole('checkbox', {name: 'Codex'}).check();
  await shoot('Codex on');
  await page.getByRole('table', {name: 'Models'}).getByRole('row').nth(1).click();
  await shoot('model detail');
  await page.getByRole('dialog').getByRole('button', {name: 'Close'}).click();
  await page
    .getByRole('table', {name: 'Models'})
    .getByRole('button', {name: 'Price'})
    .first()
    .click();
  await shoot('price dialog, automatic');
  await page.getByRole('dialog').getByRole('checkbox', {name: 'Automatic'}).uncheck();
  await page.getByRole('dialog').getByLabel('Output per 1M tokens').fill('-1');
  await shoot('price dialog, invalid output');
  await page.getByRole('dialog').getByLabel('Output per 1M tokens').fill('30');
  await page.getByRole('dialog').getByRole('button', {name: 'Save'}).click();
  await expect(page.getByRole('dialog').getByText('Saved', {exact: true})).toBeVisible();
  await shoot('price dialog, saved');
  await page.getByRole('dialog').getByRole('button', {name: 'Close'}).click();
  await shoot('price set');
  writeFileSync(`${dir}/contact-sheet.html`, contactSheetHtml(entries, 3));
  const sheet = await browser.newPage({viewport: {width: 1800, height: 1000}});
  await sheet.goto(`file://${dir}/contact-sheet.html`);
  await sheet.screenshot({path: `${dir}/contact-sheet.png`, fullPage: true});
  await sheet.close();
});

test('a model no price covers reads Unpriced, and neither it nor its provider is drawn at zero', async ({
  page,
}) => {
  await open(page, caseNamed('unpriced-models'), 1440);
  const models = page.getByRole('table', {name: 'Models'});
  const row = models.getByRole('row').filter({hasText: 'Gpt Next Mini'});
  await expect(row).toContainText('Unpriced');
  await expect(row).not.toContainText('$0.00');
  const providers = page.getByRole('table', {name: 'Providers'});
  await expect(providers.getByRole('row').filter({hasText: 'Codex'})).toContainText('Unpriced');
  // One layer is drawn: the provider with prices.
  await expect(page.locator('.vs-area-chart-layer')).toHaveCount(1);
  // Every name is the catalogue's or the id in words, never the id.
  await expect(page.getByText('gpt-next-mini')).toHaveCount(0);
});

test('the tokens tab carries no dollars', async ({page}) => {
  await open(page, caseNamed('one-run'), 1440);
  await page.getByRole('radio', {name: 'Tokens'}).check({force: true});
  for (const name of ['Cache savings', 'Per new best', 'Per hour now']) {
    await expect(page.getByText(name, {exact: true})).toHaveCount(0);
  }
  await page.getByRole('radio', {name: 'Cost'}).check({force: true});
  await expect(page.getByText('Per new best', {exact: true})).toBeVisible();
});

test('the tiles share a top line at every width', async ({page}) => {
  await open(page, caseNamed('one-run'), 1440);
  for (const width of WIDTHS) {
    await page.setViewportSize({width, height: 900});
    await settled(page);
    const tops = await page.evaluate(() => {
      const caption = [...document.querySelectorAll('main .vs-text')].find(
        each => each.textContent === 'Processed',
      );
      const row = caption?.parentElement?.parentElement;
      return [...(row?.children ?? [])].map(tile =>
        Math.round(tile.querySelector('.vs-text')?.getBoundingClientRect().top ?? -1),
      );
    });
    expect(tops.length, `${width}`).toBeGreaterThanOrEqual(5);
    // Tiles of a row share a top: a wrapped second row starts lower, but never staggers within one.
    const rows = new Map<number, number>();
    for (const top of tops) rows.set(top, (rows.get(top) ?? 0) + 1);
    expect(rows.size, `${width}: ${tops.join(',')}`).toBeLessThanOrEqual(2);
  }
});
