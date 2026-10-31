import {expect, test} from '@playwright/test';
import {fixtureNamed} from '@vibesys/run-fixtures';
import {settled} from './checks/settle.js';
import {openFixture} from './fixture-world.js';

/**
 * A finished run at the sizes of a laptop: the performance row sits above the Ideas table, which
 * takes the height of its rows (the page scrolls to them) and not a short window of its own, so at
 * least eight rows are in the list whole whatever the window's height. An idea opened from the first
 * rows moves neither the charts nor the time bar, and both stay in view.
 */

const SIZES = [
  {width: 1440, height: 900},
  {width: 1280, height: 860},
] as const;

const ROWS_IN_VIEW = 8;

for (const name of ['finished-run', 'finished-ten-ideas', 'finished-two-objectives']) {
  for (const size of SIZES) {
    test(`${name} at ${size.width} by ${size.height}: the list keeps its rows without a window of its own, and an idea opens in place`, async ({
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
      // A run of at most sixteen ideas has no inner scroller to be caught in.
      expect(
        await ideas.evaluate(each => each.scrollHeight - each.clientHeight),
      ).toBeLessThanOrEqual(0);
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
