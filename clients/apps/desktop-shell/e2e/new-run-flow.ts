/**
 * Driving the guided New run: choosing an answer, going on, and opening a page directly. A page is
 * opened by giving the window the draft that would have brought it there (the draft is the
 * window's own storage, which the page reads as it starts), so a check of every page does not
 * click through the pages before it.
 */
import {expect, type Page} from '@playwright/test';
import {DRAFT_KEY, storeDraft} from '@vibesys/feature-new-run/state';
import type {FakeBridgeSeed} from '../src/testing/fake-bridge.js';
import {draftAt, type NewRunPage} from '../src/testing/new-run-pages.js';
import {openNewRun, SEED} from './world.js';

export {NEW_RUN_PAGES, type NewRunPage} from '../src/testing/new-run-pages.js';

/** Opens the app showing `page`, as if its answers had been given in this window. */
export async function openAt(
  page: Page,
  at: NewRunPage,
  seed: FakeBridgeSeed = SEED,
): Promise<void> {
  const text = storeDraft(draftAt(at));
  await page.addInitScript(
    ([key, value]) => window.sessionStorage.setItem(key as string, value as string),
    [DRAFT_KEY, text],
  );
  await openNewRun(page, seed);
  await expect(page.getByRole('navigation', {name: 'New run steps'})).toBeVisible();
  // The bundled fonts swap in after first paint (`font-display: swap`), and a width measured
  // before the swap differs by a pixel from one measured after it. The first layout has started
  // every font load the page needs, so waiting for the loads to finish makes sizes final.
  await page.evaluate(() => document.fonts.ready);
}

export const main = (page: Page) => page.getByRole('main');
export const question = (page: Page, title: string | RegExp) =>
  main(page).getByRole('heading', {level: 1, name: title});

/** Choose `name` in the question's group `group`. */
export async function choose(page: Page, group: string, name: string | RegExp): Promise<void> {
  await main(page)
    .getByRole('radiogroup', {name: group, exact: true})
    .getByRole('radio', {name})
    .check();
}

/** Press Continue (or `label`) and wait for the page after it. */
export async function proceed(page: Page, label = 'Continue'): Promise<void> {
  await main(page).getByRole('button', {name: label, exact: true}).click();
}
