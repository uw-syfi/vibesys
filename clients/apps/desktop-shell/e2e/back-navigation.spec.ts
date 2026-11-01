/**
 * Back and Forward over the whole location, with the mouse's own buttons: the window's history
 * holds the screen, the run and its zoom, so Back leaves a run for where it was opened from. The
 * buttons are pressed through the browser's input pipeline, as a mouse presses them, so a press
 * the engine also handled would move two entries.
 */
import {expect, type Page, test} from '@playwright/test';
import {settled} from './checks/settle.js';
import {
  type FakeBridgeSeed,
  installFakeBridge,
  installFakeMachines,
  installFakeProjectRuns,
  MAC_CHROME,
} from './fake-bridge.js';
import {addInitScripts, initScript} from './init-scripts.js';
import {journeyScript} from './journey-world.js';
import {showScreen, sidebar} from './read.js';
import {STARTED_AT} from './started-at.js';

const PROJECT = '/home/me/vibesys-projects/queue-rs';
const RUN_ID = '20261015-163047';
const INSTANCE = '00000000000a';

const SEED: FakeBridgeSeed = {
  theme: 'ink',
  chromeInset: MAC_CHROME,
  appearance: {mode: 'dark', light: 'paper', dark: 'ink'},
  listings: [
    {
      kind: 'listed',
      host: 'ssh:host-b',
      live: [
        {
          id: INSTANCE,
          projectRoot: PROJECT,
          runId: RUN_ID,
          status: 'running',
          phase: 'running',
          paused: null,
          end: null,
          startedAt: STARTED_AT,
          reason: null,
        },
      ],
      ended: [],
      unreadable: [],
      unverified: [],
    },
  ],
  recent: [
    {
      host: 'ssh:host-b',
      hostLabel: 'host-b',
      project: PROJECT,
      task: 'mpmc',
      runId: RUN_ID,
      instanceId: INSTANCE,
      attachedAt: 3,
    },
  ],
};

test.use({colorScheme: 'dark', viewport: {width: 1440, height: 900}});

async function openShell(page: Page): Promise<void> {
  await addInitScripts(page, [
    initScript(installFakeBridge, SEED),
    initScript(installFakeProjectRuns, SEED),
    initScript(installFakeMachines, SEED),
    journeyScript('trace'),
  ]);
  await page.goto('./shell.html');
}

/** Presses a mouse button the DOM calls `back` or `forward`, as a mouse does. */
async function press(page: Page, button: 'back' | 'forward'): Promise<void> {
  const client = await page.context().newCDPSession(page);
  const at = {x: 700, y: 450, button, clickCount: 1} as const;
  await client.send('Input.dispatchMouseEvent', {type: 'mousePressed', ...at});
  await client.send('Input.dispatchMouseEvent', {type: 'mouseReleased', ...at});
  await client.detach();
}

const dashboard = (page: Page) => page.getByRole('main').getByRole('heading', {name: 'Runs'});
const run = (page: Page) => page.getByRole('main').getByRole('button', {name: /^queue-rs · mpmc/});
const ideas = (page: Page) => page.getByRole('listbox', {name: 'Ideas'});
const opened = (page: Page) => page.getByRole('heading', {name: 'Combine W1 ring with W2 padding'});
const crumbs = (page: Page) => page.getByRole('navigation', {name: 'Zoom'});

async function openIdea(page: Page): Promise<void> {
  await run(page).click();
  await ideas(page)
    .getByRole('option', {name: /Combine W1 ring/})
    .click();
  await expect(opened(page)).toBeVisible();
}

test('the mouse buttons retrace a run’s zooms, leave it for the dashboard, and return', async ({
  page,
}) => {
  await openShell(page);
  await openIdea(page);
  await press(page, 'back');
  await expect(opened(page)).toHaveCount(0);
  await expect(ideas(page)).toBeVisible();
  await press(page, 'back');
  await expect(dashboard(page)).toBeVisible();
  await press(page, 'forward');
  await expect(ideas(page)).toBeVisible();
  await press(page, 'forward');
  await expect(opened(page)).toBeVisible();
});

test('the Runs crumb goes up to the dashboard from any level, and Back returns to that level', async ({
  page,
}) => {
  await openShell(page);
  await openIdea(page);
  // The row of crumbs, with the way up to the dashboard first, shows from the idea's steps on.
  await page
    .getByRole('listbox', {name: /steps$/})
    .getByRole('option')
    .first()
    .click();
  await crumbs(page).getByRole('button', {name: 'Runs', exact: true}).click();
  await expect(dashboard(page)).toBeVisible();
  await page.goBack();
  await expect(crumbs(page)).toBeVisible();
});

test('opening an idea moves neither the charts nor the time bar', async ({page}) => {
  await openShell(page);
  await run(page).click();
  const bar = page.getByRole('slider', {name: 'Run position'});
  const chart = page.locator('.vs-chart-panel').first();
  await expect(ideas(page)).toBeVisible();
  await settled(page);
  const before = {bar: await bar.boundingBox(), chart: await chart.boundingBox()};
  await ideas(page)
    .getByRole('option', {name: /Combine W1 ring/})
    .click();
  await expect(opened(page)).toBeVisible();
  await settled(page);
  // The idea opens beside the list, which gives it room: the list narrows, the performance row stays.
  expect({bar: await bar.boundingBox(), chart: await chart.boundingBox()}).toEqual(before);
});

test('Back crosses screens, and each screen keeps one entry', async ({page}) => {
  await openShell(page);
  const rail = sidebar(page);
  const machines = rail.getByRole('button', {name: /^Machines/});
  await machines.click();
  await machines.click();
  await showScreen(page, 'Settings');
  await showScreen(page, 'Settings');
  await page.getByRole('main').getByRole('button', {name: 'Back'}).click();
  await expect(page).toHaveURL(/#machines$/);
  await page.goBack();
  await expect(dashboard(page)).toBeVisible();
  await page.goForward();
  await page.goForward();
  await expect(page).toHaveURL(/#settings$/);
});

test('a reload at a zoom shows the same run at the same zoom, and Back still leaves it', async ({
  page,
}) => {
  await openShell(page);
  await openIdea(page);
  await page.reload();
  await expect(opened(page)).toBeVisible();
  await press(page, 'back');
  await expect(ideas(page)).toBeVisible();
  await press(page, 'back');
  await expect(dashboard(page)).toBeVisible();
});

test('leaving a zoomed run keeps its zoom from the next run opened', async ({page}) => {
  await openShell(page);
  await openIdea(page);
  await showScreen(page, 'Runs');
  await expect(dashboard(page)).toBeVisible();
  await expect(page).not.toHaveURL(/idea=/);
  await run(page).click();
  await expect(ideas(page)).toBeVisible();
  await expect(opened(page)).toHaveCount(0);
});

/** The offsets of every element on the page that scrolls, by its class. */
const scrolls = (page: Page) =>
  page.evaluate(() =>
    Object.fromEntries(
      [...document.querySelectorAll<HTMLElement>('*')]
        .filter(
          element =>
            element.scrollHeight > element.clientHeight &&
            ['auto', 'scroll'].includes(getComputedStyle(element).overflowY),
        )
        .map(element => [element.className, Math.round(element.scrollTop)]),
    ),
  );

const scrollEverything = (page: Page, top: number) =>
  page.evaluate(offset => {
    for (const element of document.querySelectorAll<HTMLElement>('*'))
      if (
        element.scrollHeight > element.clientHeight &&
        ['auto', 'scroll'].includes(getComputedStyle(element).overflowY)
      )
        element.scrollTop = offset;
  }, top);

test('Back and Forward return a screen to where it was scrolled, and a new visit starts at the top', async ({
  page,
}) => {
  await page.setViewportSize({width: 1000, height: 420});
  await openShell(page);
  await run(page).click();
  await expect(ideas(page)).toBeVisible();
  await scrollEverything(page, 60);
  const onRun = await scrolls(page);
  expect(Object.values(onRun).length).toBeGreaterThan(0);
  await showScreen(page, 'Settings');
  await expect(page).toHaveURL(/#settings$/);
  await scrollEverything(page, 40);
  const onSettings = await scrolls(page);
  expect(Object.values(onSettings).length).toBeGreaterThan(0);
  await press(page, 'back');
  await expect.poll(() => scrolls(page)).toEqual(onRun);
  await press(page, 'forward');
  await expect.poll(() => scrolls(page)).toEqual(onSettings);
  await showScreen(page, 'Machines');
  await press(page, 'back');
  await expect.poll(() => scrolls(page)).toEqual(onSettings);
  // Opening a screen anew starts it at the top.
  await showScreen(page, 'Runs');
  await run(page).click();
  await expect(ideas(page)).toBeVisible();
  expect(Object.values(await scrolls(page)).every(top => top === 0)).toBe(true);
});
