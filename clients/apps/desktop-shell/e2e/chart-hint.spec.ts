/**
 * The exact numbers behind a chart's headline reach everyone, in the real browser: Tab lands on the
 * figure, Chromium's accessibility tree gives it the numbers as its description, and they are drawn
 * under it on focus and on hover (and only then), as a status chip's hint is.
 */
import {expect, type Page, test} from '@playwright/test';
import {fixtureNamed} from '@vibesys/run-fixtures';
import {openFixture} from './fixture-world.js';

const NUMBERS = '6,054,154 ops/s · input 1,581,539 ops/s';
const figure = (page: Page) => page.locator('.vs-chart-figure[aria-description]');

/** The words drawn under the figure, or `none` while they are not. */
const drawn = (page: Page) =>
  figure(page).evaluate(each => getComputedStyle(each, '::after').content);

test.beforeEach(async ({page}) => {
  await openFixture(page, fixtureNamed('finished-ten-ideas'), {theme: 'paper'});
  await expect(figure(page)).toBeVisible();
});

test('Tab reaches the headline, which then draws its numbers', async ({page}) => {
  await expect(figure(page)).toHaveAttribute('tabindex', '0');
  expect(await drawn(page)).toBe('none');
  await page.locator('body').click({position: {x: 1, y: 1}});
  let reached = false;
  for (let presses = 0; presses < 120 && !reached; presses += 1) {
    await page.keyboard.press('Tab');
    reached = await figure(page).evaluate(each => each === document.activeElement);
  }
  expect(reached, 'Tab never reached the chart headline').toBe(true);
  expect(await drawn(page)).toContain(NUMBERS);
  await page.keyboard.press('Tab');
  expect(await drawn(page)).toBe('none');
});

test('the numbers are drawn on hover and only while hovered', async ({page}) => {
  expect(await drawn(page)).toBe('none');
  await figure(page).hover();
  expect(await drawn(page)).toContain(NUMBERS);
  await page.mouse.move(1, 1);
  expect(await drawn(page)).toBe('none');
});

test('Chromium exposes the numbers as the headline’s description, and there is no title', async ({
  page,
}) => {
  await expect(figure(page)).toHaveAccessibleDescription(NUMBERS);
  await expect(figure(page)).not.toHaveAttribute('title', /.*/);
});
