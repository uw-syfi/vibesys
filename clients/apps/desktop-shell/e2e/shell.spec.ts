import AxeBuilder from '@axe-core/playwright';
import {expect, test} from '@playwright/test';
import {rawTextOf, screenCheck} from './checks/screen-check.js';
import {MAC_CHROME, WINDOWS_CHROME} from './fake-bridge.js';
import {showScreen, sidebar} from './read.js';
import {open, SEED} from './world.js';

test('Settings changes the theme at once and keeps it', async ({page}) => {
  await open(page);
  await showScreen(page, 'Settings');
  await expect(page.getByRole('heading', {name: 'Appearance'})).toBeVisible();
  await page.getByRole('radio', {name: 'Always dark'}).check();
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'ink');
  await page.getByRole('button', {name: 'Midnight'}).click();
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'midnight');
  await expect(page.getByRole('button', {name: 'Midnight'})).toHaveAttribute(
    'aria-pressed',
    'true',
  );
  await page
    .getByRole('navigation', {name: 'Settings sections'})
    .getByRole('button', {name: 'Notifications'})
    .click();
  await page.getByRole('switch', {name: /Notify me/}).uncheck();
  await expect(page.getByRole('switch', {name: /Notify me/})).not.toBeChecked();
  const axe = await new AxeBuilder({page}).analyze();
  expect(axe.violations).toEqual([]);
});

test('the sidebar and Settings draw no code names', async ({page}) => {
  await open(page);
  expect(await rawTextOf(page)).toEqual([]);
  await showScreen(page, 'Settings');
  await expect(page.getByRole('heading', {name: 'Appearance'})).toBeVisible();
  expect(await rawTextOf(page)).toEqual([]);
});

test('Match system follows the system appearance', async ({page}) => {
  await page.emulateMedia({colorScheme: 'dark'});
  await open(page, {...SEED, theme: 'ink'});
  await showScreen(page, 'Settings');
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'ink');
  await page.emulateMedia({colorScheme: 'light'});
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'paper');
});

for (const [name, chromeInset] of [
  ['macOS', MAC_CHROME],
  ['Windows and Linux', WINDOWS_CHROME],
] as const) {
  test(`on ${name} nothing is drawn under the window controls and no control moves the window, at every width`, async ({
    page,
  }) => {
    for (const width of [1280, 700]) {
      await page.setViewportSize({width, height: 860});
      await open(page, {...SEED, chromeInset});
      expect(await screenCheck(page, ['chrome-clear', 'drag-region']), `at ${width} px`).toEqual(
        [],
      );
      const rail = await sidebar(page).evaluate(element => element.getBoundingClientRect().width);
      expect(rail, `the rail is at least the corner the controls cover, at ${width} px`).toBe(
        Math.max(64, chromeInset.startWidth),
      );
    }
  });
}
