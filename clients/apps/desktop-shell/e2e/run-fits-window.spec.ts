import {expect, test} from '@playwright/test';
import {fixtureNamed} from '@vibesys/run-fixtures';
import {settled} from './checks/settle.js';
import {openFixture} from './fixture-world.js';

/**
 * A finished run fits the window: at the sizes of a laptop the performance row and the lists share
 * the screen without the page scrolling, so opening an idea moves neither the charts nor the time
 * bar, and both stay in view while the idea's detail is read. A screen too short for them scrolls
 * (the lists keep their floor), which is the one case this does not cover.
 */

const SIZES = [
  {width: 1440, height: 900},
  {width: 1280, height: 860},
] as const;

for (const name of ['finished-run', 'finished-ten-ideas', 'finished-two-objectives']) {
  for (const size of SIZES) {
    test(`${name} at ${size.width} by ${size.height}: the page does not scroll, and an idea opens in place`, async ({
      page,
    }) => {
      await page.setViewportSize(size);
      await openFixture(page, fixtureNamed(name));
      const ideas = page.getByRole('listbox', {name: 'Ideas'});
      await expect(ideas.getByRole('option').first()).toBeVisible();
      await settled(page);
      const placed = () =>
        page.evaluate(() => {
          const body = document.querySelector('.vs-screen-body');
          const top = (selector: string) =>
            document.querySelector(selector)?.getBoundingClientRect().top ?? Number.NaN;
          return {
            overflow: (body?.scrollHeight ?? 0) - (body?.clientHeight ?? 0),
            scrolled: body?.scrollTop ?? Number.NaN,
            chart: top('svg.vs-chart'),
            bar: top('input[type="range"][aria-label="Run position"]'),
          };
        });
      const before = await placed();
      expect(
        before.overflow,
        'what sits above the lists leaves them their floor',
      ).toBeLessThanOrEqual(0);
      const options = ideas.getByRole('option');
      await options.nth((await options.count()) - 1).click();
      await settled(page);
      expect(await placed()).toEqual(before);
      await expect(page.getByRole('slider', {name: 'Run position'})).toBeInViewport();
      await expect(page.locator('svg.vs-chart').first()).toBeInViewport();
    });
  }
}
