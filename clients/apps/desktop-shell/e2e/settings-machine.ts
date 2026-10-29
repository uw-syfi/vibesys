/**
 * Driving the machine-scoped Settings: opening the app on Settings with the world's machines (This
 * Mac, fully set up; host-b, with nothing set up; host-c, whose VibeSys predates both features),
 * choosing a machine and a section, and the fake helper's record of what was done.
 */
import {expect, type Page} from '@playwright/test';
import type {FakeBridgeSeed, FakeMachineSettings} from './fake-bridge.js';
import {open, SEED, sidebar} from './world.js';

const MACHINES = [
  {host: 'local', label: 'This Mac', checkout: '~/projs/vibesys'},
  {host: 'ssh:host-b', label: 'host-b', checkout: '/home/me/vibesys'},
  {host: 'ssh:host-c', label: 'host-c', checkout: '/home/me/vibesys'},
] as const;

const SECTIONS = ['Clusters and clouds', 'Agent logins'] as const;
export type SectionName = (typeof SECTIONS)[number];

export const nav = (page: Page) => page.getByRole('navigation', {name: 'Settings sections'});
export const main = (page: Page) => page.getByRole('main');

/** Opens the app on Settings at `width`, measured, with the sidebar put away where it would float. */
export async function openSettingsAt(
  page: Page,
  width: number,
  seed: FakeBridgeSeed = {...SEED, machines: MACHINES},
): Promise<void> {
  await page.setViewportSize({width, height: 900});
  await open(page, seed);
  expect(await page.evaluate(() => window.innerWidth), 'the window is as wide as asked').toBe(
    width,
  );
  await sidebar(page).getByRole('button', {name: 'Settings'}).click();
}

/** Opens the section list where the window is narrow enough to fold it under one line. */
export async function revealNav(page: Page): Promise<void> {
  const toggle = nav(page).locator('.vs-section-toggle');
  if ((await toggle.isVisible()) && (await toggle.getAttribute('aria-expanded')) === 'false') {
    await toggle.click();
  }
}

/** Folds the section list again where it folds, as a person does once they have chosen. */
async function foldNav(page: Page): Promise<void> {
  const toggle = nav(page).locator('.vs-section-toggle');
  if ((await toggle.isVisible()) && (await toggle.getAttribute('aria-expanded')) === 'true') {
    await toggle.click();
  }
}

/** Opens `section` of the machine `machine` (its label in the selector). */
export async function openSection(
  page: Page,
  section: SectionName,
  machine: string | null = null,
): Promise<void> {
  await revealNav(page);
  await nav(page).getByRole('button', {name: section}).click();
  if (machine !== null) {
    await nav(page).getByRole('combobox', {name: 'Machine'}).selectOption({label: machine});
  }
  await foldNav(page);
  await expect(main(page).getByRole('heading', {level: 1, name: section})).toBeVisible();
}

/** What the fake helper was asked to do, as the journey reads it. */
export async function done(page: Page): Promise<FakeMachineSettings> {
  return page.evaluate(
    () =>
      (window as unknown as {fakeHost: {machineSettings: FakeMachineSettings}}).fakeHost
        .machineSettings,
  );
}
