/**
 * The pages of machine-scoped Settings, each reached the way a user reaches it, so the layout
 * checks look at the screens the DOM tests audit: the steps are `src/testing/settings-pages.ts`'s,
 * read here with a browser.
 */
import {expect, type Page} from '@playwright/test';
import {SETTINGS_PAGES as PAGES, type Step} from '../src/testing/settings-pages.js';
import {main, openSection} from './settings-machine.js';

export interface SettingsPage {
  readonly name: string;
  /** Brings the page up, from the Settings screen just opened. */
  reach(page: Page): Promise<void>;
}

async function take(page: Page, step: Step): Promise<void> {
  if ('open' in step) {
    await openSection(page, step.open, step.machine ?? null);
  } else if ('press' in step) {
    await main(page).getByRole(step.role, {name: step.press}).first().click();
  } else if ('choose' in step) {
    await main(page).getByRole('radio', {name: step.choose}).check();
  } else if ('fill' in step) {
    await main(page).getByRole('textbox', {name: step.fill}).fill(step.text);
  } else if (step.heading === true) {
    await expect(main(page).getByRole('heading', {name: step.see})).toBeVisible();
  } else if (step.inAlert === true) {
    await expect(main(page).getByRole('alert').getByText(step.see)).toBeVisible();
  } else {
    await expect(main(page).getByText(step.see, {exact: true})).toBeVisible();
  }
}

export const SETTINGS_PAGES: readonly SettingsPage[] = PAGES.map(each => ({
  name: each.name,
  reach: async page => {
    for (const step of each.steps) await take(page, step);
  },
}));
