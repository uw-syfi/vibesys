import {mkdirSync, writeFileSync} from 'node:fs';
import AxeBuilder from '@axe-core/playwright';
import {expect, type Page, test} from '@playwright/test';
import {
  installFakeBridge,
  installFakeChrome,
  installFakeMachines,
  installFakeNewRun,
  installFakeProjectRuns,
  installFakeUsage,
  installFakeUsageLimits,
  installFakeUsagePrices,
} from '../src/testing/fake-bridge.js';
import {contactSheetHtml, type SheetEntry} from './checks/contact-sheet.js';
import {KnownLedger} from './checks/known-ledger.js';
import {SCREEN_CHECKS, screenCheck} from './checks/screen-check.js';
import {settled} from './checks/settle.js';
import {addInitScripts, initScript} from './init-scripts.js';
import {nativeFixtureScript} from './native-fixture.js';
import {showScreen} from './read.js';
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
    initScript(installFakeUsageLimits, seed),
    nativeFixtureScript(runFixtureFor(usage)),
  ]);
  await page.goto('./shell.html');
  await showScreen(page, 'Usage');
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

ledger.requireEveryFailure();

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

test('the run rows open with the keyboard too', async ({page}) => {
  await open(page, caseNamed('one-run'), 1000);
  const row = page.getByRole('table', {name: 'Cost by run'}).getByRole('row').nth(1);
  await row.focus();
  await page.keyboard.press('Enter');
  await expect(page.getByRole('heading', {name: 'Usage', level: 1})).toHaveCount(0);
  expect(await faceOf(page)).toMatch(/^#run=[^&]+&mode=usage$/);
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

test('the Price button of a model is on screen at 660 px', async ({page}) => {
  await open(page, caseNamed('one-run'), 660);
  const price = page
    .getByRole('table', {name: 'Models'})
    .getByRole('button', {name: 'Price'})
    .first();
  await price.scrollIntoViewIfNeeded();
  const box = await price.boundingBox();
  const width = await page.evaluate(() => window.innerWidth);
  expect(box).not.toBeNull();
  expect(
    (box?.x ?? -1) >= 0 && (box?.x ?? 0) + (box?.width ?? 0) <= width,
    JSON.stringify(box),
  ).toBe(true);
  await price.click();
  await expect(page.getByRole('dialog')).toBeVisible();
});

for (const width of WIDTHS) {
  test(`the Limits tab passes the screen checks at ${width} px`, async ({page}) => {
    await open(page, caseNamed('one-run'), width);
    await page.getByRole('radio', {name: 'Limits'}).check({force: true});
    await expect(page.getByRole('heading', {name: 'Claude', level: 2})).toBeVisible();
    await settled(page);
    expect(
      ledger.unexplained(`usage/limits/${width}`, await screenCheck(page, SCREEN_CHECKS)),
    ).toEqual([]);
    const overflow = await page.evaluate(
      () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
    );
    expect(overflow, `${width}`).toBeLessThanOrEqual(0);
  });
}
