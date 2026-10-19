import {expect, type Page, test} from '@playwright/test';
import {fixtureNamed} from '@vibesys/run-fixtures';
import {settled} from './checks/settle.js';
import {openFixture, playLive} from './fixture-world.js';

/**
 * The Usage face of the Run screen on a finished run whose turns report what they spent and on
 * which harness and model they ran: its totals say what the header says, each control changes
 * what the table shows, the controls and the table's columns keep their rows and tops at every
 * width, and the Agents list and an idea's steps name what ran them. Every width is the page's
 * own: `innerWidth` is asserted before anything is measured.
 */

const FIXTURE = 'finished-ten-ideas';

async function open(page: Page, width: number): Promise<void> {
  await page.setViewportSize({width, height: 900});
  const fixture = fixtureNamed(FIXTURE);
  await openFixture(page, fixture, {sidebar: false});
  await playLive(page, fixture);
  await expect(page.getByRole('slider', {name: 'Run position'})).toBeVisible();
  await settled(page);
  expect(await page.evaluate(() => window.innerWidth)).toBe(width);
}

async function showUsage(page: Page): Promise<void> {
  await page.getByRole('radiogroup', {name: 'Show'}).getByText('Usage').click();
  await expect(page.getByRole('table', {name: 'Usage by group'})).toBeVisible();
  await settled(page);
}

const select = (page: Page, label: string) => page.getByLabel(label, {exact: true});

/** The rows' first cells: the groups the table lists. */
const groups = (page: Page): Promise<string[]> =>
  page
    .getByRole('table', {name: 'Usage by group'})
    .locator('tbody tr td:first-child')
    .evaluateAll(cells => cells.map(cell => (cell.textContent ?? '').trim()));

function totalsSayHeader(width: number): void {
  test('the totals say what the header says', async ({page}) => {
    await open(page, width);
    const header = await page
      .getByRole('main')
      .locator('.vs-text', {hasText: /^· in /})
      .first()
      .textContent();
    await showUsage(page);
    const fact = await page
      .locator('.vs-pane', {has: page.getByRole('table', {name: 'Usage by group'})})
      .locator('.vs-fact')
      .first()
      .textContent();
    expect((header ?? '').replace(/^· /, '')).toBe(fact);
  });
}

function everyControlActs(width: number): void {
  test('every control changes what the face shows', async ({page}) => {
    await open(page, width);
    await showUsage(page);
    const byRole = await groups(page);
    expect(byRole.length).toBeGreaterThan(0);

    await select(page, 'Group by').selectOption('harness');
    const byHarness = await groups(page);
    expect(byHarness.length).toBe(2);
    expect(byHarness).not.toEqual(byRole);

    await select(page, 'Then by').selectOption('role');
    const nested = await groups(page);
    expect(nested.every(group => group.includes('·'))).toBe(true);
    expect(nested.length).toBeGreaterThanOrEqual(byHarness.length);

    const before = await page.locator('.vs-bars-plot').innerHTML();
    await page.getByRole('radio', {name: 'Cost'}).check({force: true});
    await expect(page.getByRole('img', {name: /in US dollars/})).toBeVisible();
    expect(await page.locator('.vs-bars-plot').innerHTML()).not.toBe(before);
    expect(await page.evaluate(() => window.innerWidth)).toBe(width);
  });
}

function controlsAndColumns(width: number): void {
  test('the controls sit in one row where they fit, none clipped, and the table columns share their tops', async ({
    page,
  }) => {
    await open(page, width);
    await showUsage(page);
    const boxes = await page.evaluate(() => {
      const box = (element: Element | null) => {
        const rect = element?.getBoundingClientRect();
        return rect === undefined
          ? null
          : {top: rect.top, bottom: rect.bottom, left: rect.left, right: rect.right};
      };
      const controls = [
        ...document.querySelectorAll('.vs-field select'),
        document.querySelector('[role="radiogroup"][aria-label="Chart shows"]'),
      ];
      const heads = [...document.querySelectorAll('.vs-table thead th')].map(box);
      const cells = [...document.querySelectorAll('.vs-table tbody tr:first-child td')].map(box);
      return {
        width: window.innerWidth,
        controls: controls.map(box),
        heads,
        cells,
        page: document.scrollingElement?.scrollWidth ?? 0,
      };
    });
    expect(boxes.width).toBe(width);
    expect(boxes.page).toBeLessThanOrEqual(width);
    for (const control of boxes.controls) {
      expect(control?.left ?? -1).toBeGreaterThanOrEqual(0);
      expect(control?.right ?? width + 1).toBeLessThanOrEqual(width);
    }
    const tops = (list: ({top: number} | null)[]): number[] =>
      list.map(each => Math.round(each?.top ?? -1));
    expect(new Set(tops(boxes.heads)).size).toBe(1);
    expect(new Set(tops(boxes.cells)).size).toBe(1);
    if (width >= 1000) {
      // Labelled fields and the unlabelled switch are aligned along their bottoms.
      const bottoms = new Set(boxes.controls.map(each => Math.round(each?.bottom ?? -1)));
      expect(bottoms.size, 'Group by, Then by and the chart switch share one row').toBe(1);
    }
  });
}

function agentsAndStepsNameWho(width: number): void {
  test('the Agents list and an idea’s steps name the harness and model that ran them', async ({
    page,
  }) => {
    await open(page, width);
    await page.getByRole('radiogroup', {name: 'Show'}).getByText('Agents').click();
    const agents = page.getByRole('listbox', {name: 'Agents'});
    await expect(agents.getByRole('option').first()).toBeVisible();
    // Finished agents fold into one line; unfold them.
    const folded = agents.getByRole('option', {name: /finished/});
    if ((await folded.count()) > 0) await folded.first().click();
    const rows = await agents.getByRole('option').allTextContents();
    expect(rows.some(row => /on codex/.test(row))).toBe(true);
    expect(rows.some(row => /on claude/.test(row))).toBe(true);
    await page.getByRole('radiogroup', {name: 'Show'}).getByText('Ideas').click();
    await page
      .getByRole('listbox', {name: 'Ideas'})
      .getByRole('option', {name: /Pad the slot headers/})
      .click();
    const steps = page.getByRole('listbox', {name: /steps$/});
    await expect(steps).toBeVisible();
    expect(await steps.getByRole('option').first().textContent()).toMatch(/on (codex|claude)/);
  });
}

for (const width of [1440, 1000, 660]) {
  test.describe(`at ${width} px`, () => {
    totalsSayHeader(width);
    everyControlActs(width);
    controlsAndColumns(width);
    agentsAndStepsNameWho(width);
  });
}
