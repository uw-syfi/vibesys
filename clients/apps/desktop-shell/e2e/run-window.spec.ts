import {type BrowserContext, expect, type Page, test} from '@playwright/test';
import {
  type FakeBridgeSeed,
  type FakeWindows,
  installFakeBridge,
  installFakeMachines,
  installFakeNewRun,
  installFakeProjectRuns,
  installFakeWindows,
} from './fake-bridge.js';
import {installFakeRun} from './fake-run.js';
import {type FakeServerSeed, installFakeServer} from './fake-server.js';
import {addInitScripts, initScript} from './init-scripts.js';
import {drawnUnderWindowControls, listed, SEED, server} from './world.js';

/**
 * Open in new window: the Run screen's "⋯" and the tile's menu ask main to show the run in a
 * window of its own (the Fake opens a browser window on the run's page, as main opens an
 * `AppWindow`). That window shows the Run screen alone, for the run its URL names, and is live
 * there; the main window leaves the run for Runs. Asking again focuses the window.
 */

const RUN_ID = '20261015-163047';

const WINDOW_SEED: FakeBridgeSeed = {
  ...SEED,
  listings: [listed([server('00000000000a', RUN_ID, 'running')], [])],
};

const SERVER: FakeServerSeed = {
  runId: RUN_ID,
  batch: {
    type: 'event_batch',
    events: [{timestamp: '2026-10-15T16:30:51Z', type: 'server_ready', sequence: 1}],
    through_sequence: 1,
    store_id: 'store-1',
    history_after_sequence: 0,
  },
  snapshot: {run_id: RUN_ID, sequence: 1, status: 'running'},
};

/** Every page in `context`, the run windows too, gets the Fake bridge before its scripts. */
async function install(context: BrowserContext): Promise<void> {
  await addInitScripts(context, [
    initScript(installFakeBridge, WINDOW_SEED),
    initScript(installFakeMachines, WINDOW_SEED),
    initScript(installFakeNewRun, WINDOW_SEED),
    initScript(installFakeProjectRuns, WINDOW_SEED),
    initScript(installFakeWindows, undefined),
    initScript(installFakeRun, {streams: [], text: {}}),
    initScript(installFakeServer, SERVER),
  ]);
}

async function windows(page: Page): Promise<FakeWindows> {
  return page.evaluate(
    () => (window as unknown as {fakeHost: {windows: FakeWindows}}).fakeHost.windows,
  );
}

async function commands(page: Page): Promise<string[]> {
  return page.evaluate(() => [
    ...(window as unknown as {fakeHost: {commands: string[]}}).fakeHost.commands,
  ]);
}

const sidebar = (page: Page) => page.getByRole('navigation', {name: 'VibeSys'});

test('a run opens in a window of its own, shown alone and live there', async ({page, context}) => {
  await install(context);
  await page.goto('./shell.html');
  await page
    .getByRole('main')
    .getByRole('button', {name: /^queue-rs · mpmc/})
    .click();
  await expect(page.getByRole('radiogroup', {name: 'Show'})).toBeVisible();

  const opening = context.waitForEvent('page');
  await page.getByRole('button', {name: 'More actions'}).click();
  await page.getByRole('menuitem', {name: 'Open in new window'}).click();
  const runWindow = await opening;
  await runWindow.waitForLoadState();

  // The main window leaves the run for Runs; the new one shows the run, with no rail.
  await expect(page.getByRole('heading', {name: 'Runs'})).toBeVisible();
  expect(new URL(runWindow.url()).search).toBe(`?host=ssh%3Ahost-b&run=${RUN_ID}`);
  await expect(runWindow.getByRole('radiogroup', {name: 'Show'})).toBeVisible();
  await expect(runWindow.getByText(`host-b · ${RUN_ID}`)).toBeVisible();
  await expect(sidebar(runWindow)).toHaveCount(0);
  // Its title is clear of the window controls, below the same title-bar row as the main window's,
  // and the row drags the window.
  expect(await drawnUnderWindowControls(runWindow)).toEqual([]);
  expect(await drawnUnderWindowControls(page)).toEqual([]);
  const row = await runWindow.locator('.vs-app-strip').evaluate(element => ({
    height: element.getBoundingClientRect().height,
    drag: getComputedStyle(element).getPropertyValue('-webkit-app-region'),
  }));
  expect(row).toEqual({height: 40, drag: 'drag'});
  // The run is live in its window (it attached there), and the main window let it go.
  await expect.poll(() => commands(runWindow)).toContain('attach 00000000000a');
  await expect.poll(() => commands(page)).toContain('release');
  // It is its own window already: it does not offer another.
  await runWindow.getByRole('button', {name: 'More actions'}).click();
  await expect(runWindow.getByRole('menuitem', {name: 'Open in new window'})).toHaveCount(0);
  await runWindow.keyboard.press('Escape');

  // Asking again, from the tile's menu, focuses that window rather than opening another.
  await page.getByRole('button', {name: 'More for queue-rs · mpmc'}).click();
  await page.getByRole('menuitem', {name: 'Open in new window'}).click();
  await expect
    .poll(async () => (await windows(page)).opened)
    .toEqual([`ssh:host-b ${RUN_ID}`, `ssh:host-b ${RUN_ID}`]);
  expect(context.pages()).toHaveLength(2);

  // Closing it leaves the main window as it was.
  await runWindow.close();
  await expect(page.getByRole('heading', {name: 'Runs'})).toBeVisible();
});

test('the Settings menu item opens Settings in the main window while a run shows in its own window', async ({
  page,
  context,
}) => {
  await install(context);
  await page.goto('./shell.html');
  await page
    .getByRole('main')
    .getByRole('button', {name: /^queue-rs · mpmc/})
    .click();
  const opening = context.waitForEvent('page');
  await page.getByRole('button', {name: 'More actions'}).click();
  await page.getByRole('menuitem', {name: 'Open in new window'}).click();
  const runWindow = await opening;
  await runWindow.waitForLoadState();
  await expect(runWindow.getByRole('radiogroup', {name: 'Show'})).toBeVisible();

  // The menu belongs to the application, not to the page that has focus: it reaches the main window.
  await page.evaluate(() =>
    (window as unknown as {fakeHost: {openSettings(): void}}).fakeHost.openSettings(),
  );
  await expect(page.getByRole('heading', {name: 'Appearance'})).toBeVisible();
  await expect(runWindow.getByRole('radiogroup', {name: 'Show'})).toBeVisible();
  await expect(runWindow.getByRole('heading', {name: 'Appearance'})).toHaveCount(0);
});
