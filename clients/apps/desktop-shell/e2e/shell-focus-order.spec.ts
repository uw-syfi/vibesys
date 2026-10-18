import {expect, type Page, test} from '@playwright/test';
import {open, sidebar} from './world.js';

/**
 * The keyboard reaches the sidebar before the page, as the eye does: from a fresh load, Tab walks
 * the title-bar control and the sidebar, and only then the screen beside it, on every screen and at
 * every window width.
 */

const WIDTHS = [660, 1000, 1440] as const;
const SCREENS = ['New run', 'All runs', 'Settings'] as const;

async function reach(page: Page, screen: (typeof SCREENS)[number]): Promise<void> {
  const nav = sidebar(page);
  if (screen === 'New run') await nav.getByRole('button', {name: 'New run'}).click();
  if (screen === 'All runs') await nav.getByRole('button', {name: 'Show all'}).click();
  if (screen === 'Settings') await nav.getByRole('button', {name: 'Settings'}).click();
  // A fresh load's focus start: the click moved it, so the page's own start is restored.
  await page.evaluate(() => (document.activeElement as HTMLElement | null)?.blur());
}

for (const width of WIDTHS) {
  for (const screen of SCREENS) {
    test(`Tab reaches the sidebar before ${screen} at ${width}px`, async ({page}) => {
      await page.setViewportSize({width, height: 900});
      await open(page);
      await reach(page, screen);
      await page.evaluate(() => {
        document.body.tabIndex = -1;
        document.body.focus();
        document.body.removeAttribute('tabindex');
      });

      // One lap of Tab: the focused elements, until the first comes round again.
      const lap: {readonly id: string; readonly region: number}[] = [];
      for (let step = 0; step < 80; step += 1) {
        await page.keyboard.press('Tab');
        const focused = await page.evaluate(() => {
          const active = document.activeElement;
          const region =
            active?.closest('nav.vs-app-nav') != null ? 1 : active?.closest('main') != null ? 2 : 0;
          const path =
            active === null
              ? ''
              : `${active.tagName}|${active.getAttribute('aria-label') ?? ''}|${(active.textContent ?? '').slice(0, 30)}`;
          return {id: path, region};
        });
        // Tab out of the last control leaves the document (the body); that ends the lap.
        if (focused.id.startsWith('BODY|') || lap.some(each => each.id === focused.id)) break;
        lap.push(focused);
      }

      const regions = lap.map(each => each.region);
      expect(regions).toContain(1);
      expect(regions).toContain(2);
      // The title bar's control, then the sidebar, then the page.
      expect(regions).toEqual([...regions].sort());
    });
  }
}
