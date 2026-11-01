import {expect, type Page, test} from '@playwright/test';
import {fixtureNamed, objectivesRun, type RunFixture} from '@vibesys/run-fixtures';
import {settled} from './checks/settle.js';
import {openFixture, playLive} from './fixture-world.js';

/**
 * The Run screen's shape: a compact top (the charts side by side) and below it the whole Ideas
 * table, its header kept in view while the rows scroll and the ideas running now first. The table
 * starts in the upper part of the window and shows most of a screenful of rows, whatever the number
 * of objectives, so the reader sees ideas and not only charts. There is no pane of running ideas
 * beside it.
 */

/** The table may begin this far down the window, as a share of its height. */
const TOP_SHARE = 0.4;
const ROWS_SHOWN = 9;

const RUNS: readonly {readonly objectives: number; readonly run: RunFixture}[] = [
  {objectives: 1, run: fixtureNamed('finished-ten-ideas')},
  {objectives: 2, run: fixtureNamed('finished-two-objectives')},
  {objectives: 3, run: objectivesRun(3)},
];

/** The rows of the table whole inside the window and inside the list that scrolls them. */
async function rowsShown(page: Page): Promise<number> {
  return page.getByRole('listbox', {name: 'Ideas'}).evaluate(list => {
    const lowest = Math.min(list.getBoundingClientRect().bottom, window.innerHeight);
    const highest = list.getBoundingClientRect().top;
    return [...list.querySelectorAll('[role="option"]')].filter(row => {
      const box = row.getBoundingClientRect();
      return box.top >= highest - 1 && box.bottom <= lowest + 1;
    }).length;
  });
}

for (const {objectives, run} of RUNS) {
  for (const width of [1200, 1440]) {
    test(`${objectives} objective${objectives > 1 ? 's' : ''} at ${width}px: the table starts in the top ${TOP_SHARE * 100}% of the window and shows ${ROWS_SHOWN} rows`, async ({
      page,
    }) => {
      const height = 900;
      await page.setViewportSize({width, height});
      await openFixture(page, run);
      await expect(
        page.getByRole('listbox', {name: 'Ideas'}).getByRole('option').first(),
      ).toBeVisible();
      await settled(page);
      const table = await page.locator('.vs-columnlist').boundingBox();
      expect(table, 'the table has a box').not.toBeNull();
      expect(table?.y ?? Number.POSITIVE_INFINITY).toBeLessThanOrEqual(height * TOP_SHARE);
      const total = await page.getByRole('listbox', {name: 'Ideas'}).getByRole('option').count();
      expect(await rowsShown(page)).toBeGreaterThanOrEqual(Math.min(ROWS_SHOWN, total));
      // The charts are side by side: one row of them, however many objectives.
      const tops = await page
        .locator('.vs-chart-panel')
        .evaluateAll(panels => panels.map(panel => Math.round(panel.getBoundingClientRect().top)));
      expect(new Set(tops).size, 'the charts share one row').toBe(1);
      // Nothing runs beside the table: the Now pane is gone.
      await expect(page.getByRole('listbox', {name: 'Running now'})).toHaveCount(0);
    });
  }
}

test('the table header stays where it is while the rows scroll', async ({page}) => {
  await page.setViewportSize({width: 1200, height: 700});
  await openFixture(page, fixtureNamed('weeks-600-ideas'));
  const list = page.getByRole('listbox', {name: 'Ideas'});
  await expect(list.getByRole('option').first()).toBeVisible();
  await settled(page);
  const header = page.locator('.vs-columnlist-head');
  const before = await header.boundingBox();
  await list.evaluate(element => {
    element.scrollTop = element.scrollHeight;
  });
  await settled(page);
  const after = await header.boundingBox();
  expect(before).not.toBeNull();
  expect(after?.y).toBe(before?.y);
  expect(await list.evaluate(element => element.scrollTop)).toBeGreaterThan(0);
});

test('the ideas running now are the first rows, above every idea that is not', async ({page}) => {
  const fixture = fixtureNamed('queue-rs-live');
  await page.setViewportSize({width: 1200, height: 900});
  await openFixture(page, fixture);
  await playLive(page, fixture);
  const rows = page.getByRole('listbox', {name: 'Ideas'}).getByRole('option');
  await expect(rows.first()).toBeVisible();
  await settled(page);
  const running = await rows.evaluateAll(each =>
    each.map(row => row.querySelector('[data-column="status"]')?.textContent?.includes('Running')),
  );
  expect(running.filter(Boolean).length, 'the live run has ideas running').toBeGreaterThan(0);
  const firstOther = running.indexOf(false);
  expect(firstOther < 0 ? [] : running.slice(firstOther).filter(Boolean)).toEqual([]);
});
