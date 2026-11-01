import {expect, type Page, test} from '@playwright/test';
import {fixtureNamed} from '@vibesys/run-fixtures';
import {settled} from './checks/settle.js';
import {openFixture, playLive} from './fixture-world.js';

/**
 * The layout of the Usage face of the Run screen on a finished run whose turns report what they
 * spent: it takes the whole row, and the controls and the table's columns keep their rows and tops,
 * at 1440, 1000 and 660 px on one page resized across them. What the face says (its tiles, what
 * every control changes, the harness and model that ran each turn) is `src/run-usage.dom.test.tsx`.
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

async function layoutAt(page: Page, width: number): Promise<void> {
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
}

test('the Usage face lays out at every width: the whole row, the controls in their rows, the table at its tops', async ({
  page,
}) => {
  await open(page, 1440);
  await showUsage(page);
  for (const width of [1440, 1000, 660]) {
    await page.setViewportSize({width, height: 900});
    await settled(page);
    expect(await page.evaluate(() => window.innerWidth)).toBe(width);
    // The face takes the whole row: one pane, as wide as the panes' row.
    expect(await page.locator('.vs-panes > .vs-pane').count()).toBe(1);
    const sizes = await page.evaluate(() => ({
      pane: document.querySelector('.vs-panes > .vs-pane')?.getBoundingClientRect().width ?? 0,
      panes: document.querySelector('.vs-panes')?.getBoundingClientRect().width ?? 0,
    }));
    expect(Math.abs(sizes.pane - sizes.panes)).toBeLessThan(1);
    await layoutAt(page, width);
  }
});
