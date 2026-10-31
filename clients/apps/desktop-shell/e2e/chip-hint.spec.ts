/**
 * A chip's hint reaches everyone, in the real browser: Tab lands on the chip, Chromium's own
 * accessibility tree gives it the hint as its description, and the hint is drawn under it on focus
 * and on hover (and only then). The run's status chip stands for every chip with a hint.
 */
import {expect, type Page, test} from '@playwright/test';
import {fixtureNamed} from '@vibesys/run-fixtures';
import {openFixture} from './fixture-world.js';

const REASON = fixtureNamed('crashed-after-turn').end?.reason ?? '';
const chip = (page: Page) =>
  page.getByRole('main').locator('.vs-chip[aria-description]', {hasText: 'Failed'});

/** The text the hint is drawn as under the chip, or `none` while it is not drawn. */
const drawn = (page: Page) =>
  chip(page).evaluate(each => getComputedStyle(each, '::after').content);

test.beforeEach(async ({page}) => {
  await openFixture(page, fixtureNamed('crashed-after-turn'), {theme: 'paper'});
  await expect(chip(page)).toBeVisible();
});

test('Tab reaches the chip, which then draws its hint', async ({page}) => {
  await expect(chip(page)).toHaveAttribute('tabindex', '0');
  expect(await drawn(page)).toBe('none');
  await page.locator('body').click({position: {x: 1, y: 1}});
  let reached = false;
  for (let presses = 0; presses < 80 && !reached; presses += 1) {
    await page.keyboard.press('Tab');
    reached = await chip(page).evaluate(each => each === document.activeElement);
  }
  expect(reached, 'Tab never reached the status chip').toBe(true);
  // Keyboard focus draws the hint; it is the whole reason, as one block under the chip.
  expect(await drawn(page)).toContain(REASON.slice(0, 40));
  await page.keyboard.press('Tab');
  expect(await drawn(page)).toBe('none');
});

test('the hint is on screen, not clipped by the row that holds the chip', async ({page}) => {
  const box = await chip(page).boundingBox();
  if (box === null) throw new Error('the status chip has no box');
  // Where the hint is drawn: just under the chip, over what the page shows there.
  const clip = {x: box.x, y: box.y + box.height + 2, width: 300, height: 48};
  const before = await page.screenshot({clip});
  await chip(page).focus();
  await page.keyboard.press('Shift+Tab');
  await page.keyboard.press('Tab');
  expect(await drawn(page)).not.toBe('none');
  const after = await page.screenshot({clip});
  expect(after.equals(before), 'nothing was drawn under the chip: its hint is clipped').toBe(false);
});

test('the hint is drawn on hover and only while hovered', async ({page}) => {
  expect(await drawn(page)).toBe('none');
  await chip(page).hover();
  expect(await drawn(page)).toContain(REASON.slice(0, 40));
  await page.mouse.move(1, 1);
  expect(await drawn(page)).toBe('none');
});

test('Chromium exposes the hint as the chip’s description, and the chip has no title', async ({
  page,
}) => {
  await expect(chip(page)).toHaveAccessibleDescription(REASON);
  await expect(chip(page)).not.toHaveAttribute('title', /.*/);
  const cdp = await page.context().newCDPSession(page);
  await cdp.send('Accessibility.enable');
  const {nodes} = await cdp.send('Accessibility.getFullAXTree');
  // The chip is the one focusable node the hint describes (the words beside it carry a title too).
  const reachable = nodes.filter(
    node =>
      node.description?.value === REASON &&
      node.properties?.some(each => each.name === 'focusable' && each.value.value === true),
  );
  expect(
    reachable.length,
    'no focusable node of the accessibility tree is described by the hint',
  ).toBe(1);
});
