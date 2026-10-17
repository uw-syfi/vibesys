import {expect, test} from '@playwright/test';
import {fixtureNamed} from '@vibesys/run-fixtures';
import {openFixture, playLive} from './fixture-world.js';

/**
 * Behaviours of the Run screen that the screen checks cannot decide: what a click on the first row
 * of a long Ideas tree does, and that the Now pane keeps the role and the idea's name legible in a
 * narrow window. Both read the fixtures the screen checks walk.
 */

test('on a 600-idea run the first row says what a click does, and one click does it', async ({
  page,
}) => {
  await page.setViewportSize({width: 1440, height: 900});
  const fixture = fixtureNamed('weeks-600-ideas');
  await openFixture(page, fixture);
  await playLive(page, fixture);
  const ideas = page.getByRole('listbox', {name: 'Ideas'});
  const first = ideas.getByRole('option').first();
  await expect(first).toBeVisible();
  // The first row is the fold of the older ideas: it shows a chevron, so a click reads as "open
  // this" and opens it in place (it is not an idea, so no idea opens).
  await expect(first).toContainText(/earlier/);
  await expect(first.locator('.vs-pickrow-fold[data-fold="closed"]')).toBeVisible();
  const before = await ideas.getByRole('option').count();
  await first.click();
  await expect(first).not.toContainText(/earlier/);
  expect(await ideas.getByRole('option').count()).toBeGreaterThanOrEqual(before);
  await expect(page.getByRole('navigation', {name: 'Zoom'})).toHaveCount(0);
  // The row now first is an idea: one click opens it.
  await first.click();
  await expect(page.getByRole('navigation', {name: 'Zoom'})).toBeVisible();
});

test('at 1000 px the Now pane keeps the role and the idea name readable and says the role once', async ({
  page,
}) => {
  await page.setViewportSize({width: 1000, height: 900});
  const fixture = fixtureNamed('queue-rs-live');
  await openFixture(page, fixture);
  await playLive(page, fixture);
  const now = page.getByRole('listbox', {name: 'Running now'});
  const rows = now.getByRole('option');
  await expect(rows.first()).toBeVisible();
  const lines = await rows.evaluateAll(options =>
    options.map(option =>
      [...option.querySelectorAll('.vs-pickrow-text > .vs-text')].map(text => ({
        text: text.textContent ?? '',
        cut: text.scrollWidth > text.clientWidth + 1,
      })),
    ),
  );
  expect(lines.length).toBeGreaterThan(0);
  for (const [name, doing] of lines) {
    expect(name?.text.length ?? 0).toBeGreaterThan(0);
    // The line under the name is the role (or what the role is doing): never cut, never doubled.
    expect(doing?.cut, `"${doing?.text}" is cut`).toBe(false);
    expect(doing?.text ?? '').not.toMatch(/^(\S+) · \1$/i);
  }
});
