import {type BrowserContext, expect, type Page, test} from '@playwright/test';
import {pressMovesWindow, screenCheck} from './checks/screen-check.js';
import {
  type FakeBridgeSeed,
  type FakeWindows,
  installFakeBridge,
  installFakeChrome,
  installFakeMachines,
  installFakeNewRun,
  installFakeProjectRuns,
  installFakeWindows,
  MAC_CHROME,
} from './fake-bridge.js';
import {addInitScripts, initScript} from './init-scripts.js';
import {journeyScript} from './journey-world.js';
import {installGatewayOnly} from './retired-run-bridge.js';
import {listed, SEED, server} from './world.js';

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

/** Every page in `context`, the run windows too, gets the Fake bridge before its scripts. */
async function install(context: BrowserContext): Promise<void> {
  await addInitScripts(context, [
    initScript(installFakeChrome, WINDOW_SEED),
    initScript(installFakeBridge, WINDOW_SEED),
    initScript(installFakeMachines, WINDOW_SEED),
    initScript(installFakeNewRun, WINDOW_SEED),
    initScript(installFakeProjectRuns, WINDOW_SEED),
    initScript(installFakeWindows, undefined),
    initScript(installGatewayOnly, undefined),
    journeyScript('empty'),
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
  // Its page is clear of the window controls: it starts at the top of the window, beside an empty
  // column as wide as the main window's rail, which holds the lights and moves the window.
  expect(await screenCheck(runWindow, ['chrome-clear', 'drag-region'])).toEqual([]);
  expect(await screenCheck(page, ['chrome-clear', 'drag-region'])).toEqual([]);
  const place = await runWindow.locator('.vs-app-content').evaluate(element => {
    const {top, left} = element.getBoundingClientRect();
    return {top, left};
  });
  expect(place).toEqual({top: 0, left: MAC_CHROME.startWidth});
  expect(
    await pressMovesWindow(runWindow, [
      [10, 10],
      [MAC_CHROME.startWidth / 2, 300],
    ]),
  ).toEqual([true, true]);
  // The run is live in its window (it attached there), and the main window let it go.
  expect(await commands(runWindow)).toEqual([]);
  await expect
    .poll(() =>
      runWindow.evaluate(() =>
        (window as unknown as {fakeGateway: {active(): number}}).fakeGateway.active(),
      ),
    )
    .toBeGreaterThan(0);
  await expect
    .poll(() =>
      page.evaluate(() =>
        (window as unknown as {fakeGateway: {active(): number}}).fakeGateway.active(),
      ),
    )
    .toBe(0);
  expect(await commands(page)).toEqual([]);
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
