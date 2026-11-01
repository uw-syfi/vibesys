import {expect, test} from '@playwright/test';
import {fixtureNamed} from '@vibesys/run-fixtures';
import {settled} from './checks/settle.js';
import {openFixture} from './fixture-world.js';

/**
 * A finished run at the sizes of a laptop: the performance row sits above the Ideas table, which
 * fills the rest of the window and scrolls its rows under a header that stays, so at least nine rows
 * are in the list whole and the list ends inside the window. An idea opened from the first rows
 * moves neither the charts nor the time bar, and both stay in view.
 */

const SIZES = [
  {width: 1440, height: 900},
  {width: 1280, height: 860},
] as const;

const ROWS_IN_VIEW = 9;

for (const name of ['finished-run', 'finished-ten-ideas', 'finished-two-objectives']) {
  for (const size of SIZES) {
    test(`${name} at ${size.width} by ${size.height}: the list keeps its rows inside the window, and an idea opens in place`, async ({
      page,
    }) => {
      await page.setViewportSize(size);
      await openFixture(page, fixtureNamed(name));
      const ideas = page.getByRole('listbox', {name: 'Ideas'});
      const options = ideas.getByRole('option');
      await expect(options.first()).toBeVisible();
      await settled(page);
      const count = await options.count();
      const shortest = Math.min(
        ...(await options.evaluateAll(rows =>
          rows.map(each => each.getBoundingClientRect().height),
        )),
      );
      const list = await ideas.boundingBox();
      expect(
        list?.height ?? 0,
        `${Math.min(count, ROWS_IN_VIEW)} rows of the list at ${size.width} by ${size.height}`,
      ).toBeGreaterThanOrEqual(Math.min(count, ROWS_IN_VIEW) * shortest);
      // The list ends inside the window: the reader never scrolls the page to reach its last row.
      expect((list?.y ?? 0) + (list?.height ?? 0)).toBeLessThanOrEqual(size.height);
      const placed = () =>
        page.evaluate(() => {
          const body = document.querySelector('.vs-screen-body');
          const top = (selector: string) =>
            document.querySelector(selector)?.getBoundingClientRect().top ?? Number.NaN;
          return {
            scrolled: body?.scrollTop ?? Number.NaN,
            chart: top('svg.vs-chart'),
            bar: top('input[type="range"][aria-label="Run position"]'),
          };
        });
      const before = await placed();
      await options.first().click();
      await settled(page);
      expect(await placed()).toEqual(before);
      await expect(page.getByRole('slider', {name: 'Run position'})).toBeInViewport();
      await expect(page.locator('svg.vs-chart').first()).toBeInViewport();
    });
  }
}
