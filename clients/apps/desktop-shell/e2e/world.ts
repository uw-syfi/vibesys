/** The journeys' world: a seeded Fake bridge, and opening the shell page over it. */

import {expect, type Page} from '@playwright/test';
import type {ChromeInset} from '@vibesys/ipc';
import {storeRunFixture} from '@vibesys/run-fixtures';
import {
  type FakeBridgeSeed,
  type FakeNewRun,
  installFakeBridge,
  installFakeChrome,
  installFakeExampleDestination,
  installFakeMachineSettings,
  installFakeMachines,
  installFakeNewRun,
  installFakeProjectRuns,
} from './fake-bridge.js';
import {installGateway, wire} from './fake-gateway.js';
import {addInitScripts, initScript} from './init-scripts.js';
import {showScreen, sidebar, whenRead} from './read.js';
import {SEED} from './seed.js';

export interface FakeHostWindow extends Window {
  fakeHost: {
    readonly newRun: FakeNewRun;
    listing(listing: unknown): void;
    readonly retried: readonly string[];
    openSettings(): void;
    recent(runs: readonly unknown[]): void;
  };
}

export {listed, PROJECT, SEED, server} from './seed.js';

export async function open(page: Page, seed: FakeBridgeSeed = SEED): Promise<void> {
  await addInitScripts(page, [
    initScript(installFakeChrome, seed),
    initScript(installFakeBridge, seed),
    initScript(installFakeMachines, seed),
    initScript(installFakeMachineSettings, seed),
    initScript(installFakeNewRun, seed),
    initScript(installFakeProjectRuns, seed),
    initScript(installFakeExampleDestination, seed),
  ]);
  await page.goto('./shell.html');
  await expect(sidebar(page)).toBeVisible();
  await whenRead(page);
}

/** Goes to New run by the rail, as a user does, and returns once the screen has read what it shows. */
export const showNewRun = (page: Page): Promise<void> => showScreen(page, 'New');

/** Opens the app and goes to New run by the rail, as a user starts a run. */
export async function openNewRun(page: Page, seed: FakeBridgeSeed = SEED): Promise<void> {
  // The run that Start run opens is read over the gateway like every run.
  await installGateway(page, wire(storeRunFixture('live')));
  await open(page, seed);
  await showNewRun(page);
}

/**
 * What the host does when the window enters or leaves full screen: the controls' inset changes on
 * the root element, without a reload.
 */
export async function setChromeInset(page: Page, inset: ChromeInset): Promise<void> {
  await page.evaluate(next => {
    const root = document.documentElement;
    root.style.setProperty('--chrome-start-width', `${next.startWidth}px`);
    root.style.setProperty('--chrome-start-height', `${next.startHeight}px`);
    root.style.setProperty('--chrome-end-height', `${next.endHeight}px`);
  }, inset);
}
