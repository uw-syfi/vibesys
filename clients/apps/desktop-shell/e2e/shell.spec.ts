import AxeBuilder from '@axe-core/playwright';
import {expect, test} from '@playwright/test';
import {rawTextOf} from './checks/screen-check.js';
import {open, SEED, sidebar} from './world.js';

test('Settings changes the theme at once and keeps it', async ({page}) => {
  await open(page);
  await sidebar(page).getByRole('button', {name: 'Settings'}).click();
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
  await sidebar(page).getByRole('button', {name: 'Settings'}).click();
  await expect(page.getByRole('heading', {name: 'Appearance'})).toBeVisible();
  expect(await rawTextOf(page)).toEqual([]);
});

test('Match system follows the system appearance', async ({page}) => {
  await page.emulateMedia({colorScheme: 'dark'});
  await open(page, {...SEED, theme: 'ink'});
  await sidebar(page).getByRole('button', {name: 'Settings'}).click();
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'ink');
  await page.emulateMedia({colorScheme: 'light'});
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'paper');
});

test('nothing drawn sits under the window controls, whose strip drags the window', async ({
  page,
}) => {
  for (const width of [1280, 700]) {
    await page.setViewportSize({width, height: 860});
    await open(page);
    const overlaps = await page.evaluate(() => {
      const style = getComputedStyle(document.documentElement);
      const inset = {
        width: Number.parseFloat(style.getPropertyValue('--chrome-inset-width')),
        height: Number.parseFloat(style.getPropertyValue('--chrome-inset-height')),
      };
      // Every control, and every element that draws text of its own.
      const drawn = [...document.querySelectorAll('body *')].filter(
        element =>
          element.matches('a, button, input, select, textarea, [tabindex]:not([tabindex="-1"])') ||
          [...element.childNodes].some(
            node => node.nodeType === Node.TEXT_NODE && node.textContent?.trim() !== '',
          ),
      );
      return drawn.flatMap(element => {
        const box = element.getBoundingClientRect();
        const hit = box.width > 0 && box.left < inset.width && box.top < inset.height;
        return hit ? [`${element.textContent?.trim()} at ${box.left},${box.top}`] : [];
      });
    });
    expect(overlaps, `at ${width} px`).toEqual([]);
    const drag = await sidebar(page).evaluate(element =>
      getComputedStyle(element).getPropertyValue('-webkit-app-region'),
    );
    expect(drag).toBe('drag');
  }
});
