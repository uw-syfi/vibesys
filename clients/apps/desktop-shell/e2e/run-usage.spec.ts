import {expect, type Page, test} from '@playwright/test';
import {fixtureNamed} from '@vibesys/run-fixtures';
import {settled} from './checks/settle.js';
import {openFixture, playLive} from './fixture-world.js';

/**
 * The Usage face of the Run screen on a finished run whose turns report what they spent and on
 * which harness and model they ran: it takes the whole row, its tiles say what the header says,
 * every control changes what the face shows, the controls and the table's columns keep their rows
 * and tops at every width, and the Agents list, an idea's steps and the header name what ran them.
 * Every width is the page's own: `innerWidth` is asserted before anything is measured.
 */

const FIXTURE = 'finished-ten-ideas';

async function open(page: Page, width: number): Promise<void> {
  await page.setViewportSize({width, height: 900});
  const fixture = fixtureNamed(FIXTURE);
  await openFixture(page, fixture);
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

const groupBy = (page: Page, label: string) =>
  page.getByRole('radiogroup', {name: 'Group by'}).getByText(label, {exact: true});

/** The rows' first cells: the groups the table lists, the total last. */
const groups = (page: Page): Promise<string[]> =>
  page
    .getByRole('table', {name: 'Usage by group'})
    .locator('tbody tr td:first-child')
    .evaluateAll(cells => cells.map(cell => (cell.textContent ?? '').trim()));

function wholeWidthAndTiles(width: number): void {
  test('takes the whole row, and its tiles say what the header says', async ({page}) => {
    await open(page, width);
    const header = await page.locator('.vs-stat', {hasText: 'Tokens'}).first().innerText();
    await showUsage(page);
    expect(await page.locator('.vs-panes > .vs-pane').count()).toBe(1);
    const sizes = await page.evaluate(() => ({
      pane: document.querySelector('.vs-panes > .vs-pane')?.getBoundingClientRect().width ?? 0,
      panes: document.querySelector('.vs-panes')?.getBoundingClientRect().width ?? 0,
    }));
    expect(Math.abs(sizes.pane - sizes.panes)).toBeLessThan(1);
    const tiles = page.getByRole('region', {name: 'Totals'});
    for (const name of ['Cost', 'Tokens', 'Cached input', 'Per idea']) {
      await expect(tiles.getByText(name, {exact: true})).toBeVisible();
    }
    const input = /([\d.]+[kMB]?) input/.exec(header)?.[1] ?? 'none';
    const cached = /([\d.]+[kMB]?) cached/.exec(header)?.[1] ?? 'none';
    await expect(tiles).toContainText(`in ${input} ·`);
    await expect(tiles).toContainText(`${cached} of ${input} read from cache`);
  });
}

function everyControlActs(width: number): void {
  test('every control changes what the face shows', async ({page}) => {
    await open(page, width);
    await showUsage(page);
    const byRole = await groups(page);
    expect(byRole.at(-1)).toBe('Run total');

    await groupBy(page, 'Harness').click();
    const byHarness = await groups(page);
    expect(byHarness.length).toBe(3);
    expect(byHarness).not.toEqual(byRole);

    await groupBy(page, 'Idea').click();
    const byIdea = await groups(page);
    expect(byIdea.length).toBeGreaterThan(5);
    await expect(page.getByRole('columnheader', {name: 'result'})).toBeVisible();
    expect(await page.locator('.vs-table tbody td:nth-child(2)').allTextContents()).toContain(
      '1.31×',
    );

    await groupBy(page, 'Role').click();
    await page.getByLabel('Then by', {exact: true}).selectOption('model');
    await page
      .getByRole('button', {name: /^Expand /})
      .first()
      .click();
    expect((await groups(page)).length).toBeGreaterThan(byRole.length);

    await page.getByRole('radiogroup', {name: 'Count'}).getByText('Tokens').click();
    await expect(page.getByRole('img', {name: /in tokens/})).toBeVisible();
    const kinds = page.getByRole('checkbox');
    await expect(kinds).toHaveCount(4);
    for (const kind of ['Input', 'Cached', 'Output'])
      await page.getByLabel(kind, {exact: true}).uncheck();
    await expect(page.getByLabel('Reasoning', {exact: true})).toBeChecked();
    await page.getByLabel('Reasoning', {exact: true}).click();
    await expect(page.getByLabel('Reasoning', {exact: true})).toBeChecked();

    await page.getByRole('radiogroup', {name: 'Chart'}).getByText('Running total').click();
    await expect(page.locator('.vs-area')).toBeVisible();
    await groupBy(page, 'Harness').click();
    await page.locator('tbody tr', {hasText: 'codex'}).first().locator('td').nth(1).click();
    await expect(page.locator('tr[aria-current="true"]')).toHaveCount(1);
    await expect(page.locator('.vs-area-layer[data-dim]').first()).toBeAttached();
    expect(await page.evaluate(() => window.innerWidth)).toBe(width);
  });
}

function layoutAtWidth(width: number): void {
  test('controls stay inside the viewport and in one row where they fit, and the table keeps its tops', async ({
    page,
  }) => {
    await open(page, width);
    await showUsage(page);
    const found = await page.evaluate(() => {
      const box = (element: Element | null) => {
        const rect = element?.getBoundingClientRect();
        return rect === undefined
          ? null
          : {top: rect.top, bottom: rect.bottom, left: rect.left, right: rect.right};
      };
      const named = (label: string) =>
        document.querySelector(`[role="radiogroup"][aria-label="${label}"]`);
      return {
        width: window.innerWidth,
        controls: [
          named('Group by'),
          document.querySelector('.vs-field select'),
          named('Count'),
          named('Chart'),
        ].map(box),
        heads: [...document.querySelectorAll('.vs-table thead th')]
          .filter(each => getComputedStyle(each).display !== 'none')
          .map(box),
        cells: [...document.querySelectorAll('.vs-table tbody tr:first-child td')]
          .filter(each => getComputedStyle(each).display !== 'none')
          .map(box),
        cachedShown: [...document.querySelectorAll('.vs-table thead th')].some(
          each => each.textContent === 'cached' && getComputedStyle(each).display !== 'none',
        ),
        firstCell: box(document.querySelector('.vs-table tbody tr:first-child td:first-child')),
        page: document.scrollingElement?.scrollWidth ?? 0,
      };
    });
    expect(found.width).toBe(width);
    expect(found.page).toBeLessThanOrEqual(width);
    const first = found.firstCell;
    expect(
      (first?.right ?? 0) - (first?.left ?? 0),
      'the group names keep a readable width',
    ).toBeGreaterThanOrEqual(120);
    expect(
      (first?.bottom ?? 0) - (first?.top ?? 0),
      'a group name does not wrap letter by letter',
    ).toBeLessThan(60);
    for (const control of found.controls) {
      expect(control?.left ?? -1).toBeGreaterThanOrEqual(0);
      expect(control?.right ?? width + 1).toBeLessThanOrEqual(width);
    }
    const tops = (list: ({top: number} | null)[]): number[] =>
      list.map(each => Math.round(each?.top ?? -1));
    expect(new Set(tops(found.heads)).size).toBe(1);
    expect(new Set(tops(found.cells)).size).toBe(1);
    expect(found.cachedShown, 'Cached and Reasoning give way at narrow widths').toBe(width >= 1000);
    if (width >= 1440) {
      const bottoms = new Set(found.controls.map(each => Math.round(each?.bottom ?? -1)));
      expect(bottoms.size, 'the controls share one row').toBe(1);
    }
  });
}

function namesWhoRanIt(width: number): void {
  test('the header, the Agents list and an idea’s steps name the harness and model', async ({
    page,
  }) => {
    await open(page, width);
    await expect(
      page
        .getByRole('main')
        .getByText(/on codex/)
        .first(),
    ).toBeVisible();
    await page.getByRole('radiogroup', {name: 'Show'}).getByText('Agents').click();
    const agents = page.getByRole('listbox', {name: 'Agents'});
    await expect(agents.getByRole('option').first()).toBeVisible();
    const folded = agents.getByRole('option', {name: /finished/});
    if ((await folded.count()) > 0) await folded.first().click();
    const rows = await agents.getByRole('option').allTextContents();
    expect(rows.some(row => /on codex/.test(row))).toBe(true);
    expect(rows.some(row => /on claude/.test(row))).toBe(true);
    await page.getByRole('radiogroup', {name: 'Show'}).getByText('Ideas').click();
    await page
      .getByRole('listbox', {name: 'Ideas'})
      .getByRole('option', {name: /^Pad the slot headers/})
      .click();
    const steps = page.getByRole('listbox', {name: /steps$/});
    await expect(steps).toBeVisible();
    expect(await steps.getByRole('option').first().textContent()).toMatch(/on (codex|claude)/);
  });
}

for (const width of [1440, 1000, 660]) {
  test.describe(`at ${width} px`, () => {
    wholeWidthAndTiles(width);
    everyControlActs(width);
    layoutAtWidth(width);
    namesWhoRanIt(width);
  });
}
