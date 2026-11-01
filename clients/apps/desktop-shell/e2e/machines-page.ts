/**
 * Driving the Machines page in the browser, reached by the rail, over the worlds of
 * `src/testing/machines-world.ts`.
 */

import {expect, type Page} from '@playwright/test';
import {
  installFakeBridge,
  installFakeChrome,
  installFakeMachineSettings,
  installFakeMachines,
  installFakeProjectRuns,
} from '../src/testing/fake-bridge.js';
import {type MachinesWorld, subscriptionSeedOf} from '../src/testing/machines-world.js';
import {addInitScripts, initScript} from './init-scripts.js';
import {showScreen} from './read.js';
import {subscriptionScript} from './subscription-fake.js';

export {type MachinesWorld, WORLDS} from '../src/testing/machines-world.js';

/** The Test button of the cluster `cluster` under `machine`. */
export const testOf = (page: Page, machine: string, cluster: string) =>
  card(page, machine)
    .getByRole('row', {name: new RegExp(cluster)})
    .getByRole('button');

export const card = (page: Page, machine: string) =>
  page.locator('div.vs-card', {has: page.getByRole('heading', {name: machine, exact: true})});

/** The tests answer when `window.finishTests()` is called, so a test can be looked at under way. */
function holdTests(): void {
  const bridge = (window as unknown as {vibesysWelcome: Record<string, unknown>}).vibesysWelcome;
  const answer = bridge['testCluster'] as (...args: unknown[]) => Promise<unknown>;
  const waiting: (() => void)[] = [];
  bridge['testCluster'] = (...args: unknown[]) =>
    new Promise(resolve => waiting.push(() => resolve(answer(...args))));
  (window as unknown as {finishTests: () => void}).finishTests = () => {
    for (const release of waiting.splice(0)) release();
  };
}

/** Sets the window to `width` and reads the page's own `innerWidth` back. */
export async function resizeTo(page: Page, width: number): Promise<void> {
  await page.setViewportSize({width, height: 900});
  await expect
    .poll(() => page.evaluate(() => window.innerWidth), {message: 'the window is as wide as asked'})
    .toBe(width);
}

/** The rail: the app's one navigation. */

/** Opens the app over `world` at `width` (measured) and goes to Machines by the rail. */
export async function openMachines(page: Page, width: number, world: MachinesWorld): Promise<void> {
  const {seed} = world;
  await addInitScripts(page, [
    initScript(installFakeChrome, seed),
    initScript(installFakeBridge, seed),
    initScript(installFakeMachines, seed),
    initScript(installFakeMachineSettings, seed),
    initScript(installFakeProjectRuns, seed),
    ...(world.holdTests === true ? [initScript(holdTests, undefined)] : []),
    subscriptionScript(subscriptionSeedOf(world)),
  ]);
  await page.goto('./shell.html');
  await resizeTo(page, width);
  await showScreen(page, 'Machines');
  await expect(page.getByRole('heading', {level: 1, name: 'Machines'})).toBeVisible();
  const first = seed.machines?.[0]?.label ?? '';
  await expect(card(page, first)).toBeVisible();
  // Every machine's first answer is in before the page is looked at.
  await expect(page.getByText('Checking', {exact: true})).toHaveCount(0);
  if (world.testFirst !== undefined) {
    const {machine, cluster, shows} = world.testFirst;
    await testOf(page, machine, cluster).click();
    if (shows === 'Testing…') await expect(testOf(page, machine, cluster)).toHaveText(shows);
    else await expect(page.getByText(shows)).toBeVisible();
  }
}
