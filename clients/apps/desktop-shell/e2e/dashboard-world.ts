/**
 * The Runs dashboard's worlds (`src/testing/dashboard-world.ts`), and opening the app on one in the
 * browser.
 */

import {expect, type Page} from '@playwright/test';
import {NOW} from '../src/testing/dashboard-world.js';
import type {FakeBridgeSeed} from '../src/testing/fake-bridge.js';
import {
  installFakeBridge,
  installFakeChrome,
  installFakeDigests,
  installFakeMachineSettings,
  installFakeMachines,
  installFakeNewRun,
  installFakeProjectRuns,
} from '../src/testing/fake-bridge.js';
import {addInitScripts, initScript} from './init-scripts.js';
import {whenRead} from './read.js';

export * from '../src/testing/dashboard-world.js';

/** Opens the app on `world` and waits for it to settle on the Runs dashboard. */
export async function open(page: Page, world: FakeBridgeSeed): Promise<void> {
  await page.clock.setFixedTime(NOW);
  await addInitScripts(page, [
    initScript(installFakeChrome, world),
    initScript(installFakeBridge, world),
    initScript(installFakeMachines, world),
    initScript(installFakeMachineSettings, world),
    initScript(installFakeDigests, world),
    initScript(installFakeProjectRuns, world),
    initScript(installFakeNewRun, world),
  ]);
  await page.goto('./shell.html');
  await expect(page.getByRole('heading', {name: 'Runs'})).toBeVisible();
  await whenRead(page);
}
