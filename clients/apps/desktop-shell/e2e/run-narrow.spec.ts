import {expect, test} from '@playwright/test';
import {fixtureNamed} from '@vibesys/run-fixtures';
import {screenCheck} from './checks/screen-check.js';
import {settled} from './checks/settle.js';
import {openFixture, playLive} from './fixture-world.js';

/**
 * Behaviours of the Run screen that the screen checks cannot decide: what a click on the first row
 * of a long Ideas table does, and that the Now pane keeps the role and the idea's name legible in a
 * narrow window. Both read the fixtures the screen checks walk.
 */

test('on a 600-idea run the first row opens its idea without lineage controls', async ({page}) => {
  await page.setViewportSize({width: 1440, height: 900});
  const fixture = fixtureNamed('weeks-600-ideas');
  await openFixture(page, fixture);
  await playLive(page, fixture);
  const ideas = page.getByRole('listbox', {name: 'Ideas'});
  const first = ideas.getByRole('option').first();
  await expect(first).toBeVisible();
  await expect(first.locator('.vs-pickrow-fold, .vs-guide')).toHaveCount(0);
  expect(await ideas.getByRole('option').count()).toBeLessThan(80);
  await first.click();
  await expect(page.getByRole('button', {name: 'Close idea'})).toBeVisible();
});

test('at 1000 px a running idea keeps its name and what it is doing readable, and says the role once', async ({
  page,
}) => {
  await page.setViewportSize({width: 1000, height: 900});
  const fixture = fixtureNamed('queue-rs-live');
  await openFixture(page, fixture);
  await playLive(page, fixture);
  const rows = page
    .getByRole('listbox', {name: 'Ideas'})
    .getByRole('option')
    .filter({has: page.locator('[data-column="idea"] .vs-columnlist-lines > .vs-text')});
  await expect(rows.first()).toBeVisible();
  const lines = await rows.evaluateAll(options =>
    options.map(option =>
      [...option.querySelectorAll('[data-column="idea"] .vs-columnlist-lines > .vs-text')].map(
        text => ({
          text: text.textContent ?? '',
          cut: text.scrollWidth > text.clientWidth + 1,
        }),
      ),
    ),
  );
  expect(lines.length).toBeGreaterThan(0);
  for (const [doing] of lines) {
    // The line under the name is what the idea is doing: never cut, never doubled.
    expect(doing?.text.length ?? 0).toBeGreaterThan(0);
    expect(doing?.cut, `"${doing?.text}" is cut`).toBe(false);
    expect(doing?.text ?? '').not.toMatch(/^(\S+) · \1$/i);
  }
});

test('at 660 px an open idea takes the row from the list, and its close button brings the list back', async ({
  page,
}) => {
  await page.setViewportSize({width: 660, height: 900});
  const fixture = fixtureNamed('bases-and-effects');
  await openFixture(page, fixture);
  await playLive(page, fixture);
  const ideas = page.getByRole('listbox', {name: 'Ideas'});
  await ideas.getByRole('option', {name: /Per-core free lists/}).click();
  await expect(ideas).toBeHidden();
  const pane = page.locator('.vs-pane', {hasText: 'Per-core free lists'}).last();
  const box = await pane.boundingBox();
  expect(box?.width ?? 0).toBeGreaterThan(400);
  await expect(pane.getByText('Logs', {exact: true})).toBeInViewport({ratio: 1});
  await settled(page);
  expect(await screenCheck(page)).toEqual([]);
  await page.getByRole('button', {name: 'Close idea'}).click();
  await expect(ideas).toBeVisible();
});
