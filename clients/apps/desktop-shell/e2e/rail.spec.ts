import {expect, type Page, test} from '@playwright/test';
import {pressMovesWindow, screenCheck} from './checks/screen-check.js';
import {
  type FakeBridgeSeed,
  FULL_SCREEN_CHROME,
  installFakeBridge,
  installFakeMachines,
  installFakeProjectRuns,
  MAC_CHROME,
  WINDOWS_CHROME,
} from './fake-bridge.js';
import {addInitScripts, initScript} from './init-scripts.js';
import {journeyScript} from './journey-world.js';
import {revealNav} from './settings-machine.js';
import {STARTED_AT} from './started-at.js';
import {type FakeHostWindow, open, SEED, setChromeInset, sidebar} from './world.js';

/**
 * The rail: always there, beside every screen. Settings opened from the rail or the application
 * menu and left by Back returns to the screen it came from; its sections are peers; at a narrow
 * width its navigation stays reachable; nothing is drawn under the window controls, the rail drags
 * the window, and the page starts clear of the controls at the same height on every screen. Saved
 * runs that could not be read are said on the dashboard.
 */

const PROJECT = '/home/me/vibesys-projects/queue-rs';
const INSTANCE = '00000000000a';

const RUN_ID = '20261015-163047';

const WORLD: FakeBridgeSeed = {
  ...SEED,
  theme: 'ink',
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
  projectRuns: {
    [`ssh:host-b|${PROJECT}`]: [{runId: RUN_ID, task: 'mpmc', createdAt: STARTED_AT * 1000}],
  },
};

async function openRun(page: Page): Promise<void> {
  await addInitScripts(page, [
    initScript(installFakeBridge, WORLD),
    initScript(installFakeMachines, WORLD),
    initScript(installFakeProjectRuns, WORLD),
    journeyScript('trace'),
  ]);
  await page.goto('./shell.html');
  await page
    .getByRole('main')
    .getByRole('button', {name: /^queue-rs · mpmc/})
    .click();
  await expect(page.getByRole('slider', {name: 'Run position'})).toBeVisible();
}

function openSettingsFromMenu(page: Page) {
  return page.evaluate(() => (window as unknown as FakeHostWindow).fakeHost.openSettings());
}

test.use({colorScheme: 'dark', viewport: {width: 1440, height: 900}});

test('the rail is the one navigation: the mark, New, Runs, Machines, Usage, and Settings below', async ({
  page,
}) => {
  await open(page);
  const rail = sidebar(page);

  const names = await rail
    .getByRole('button')
    .evaluateAll(buttons =>
      buttons.map(button => button.getAttribute('aria-label') ?? button.textContent?.trim() ?? ''),
    );
  expect(names).toEqual(['VibeSys', 'New', 'Runs', 'Machines', 'Usage', 'Settings']);
  await expect(page.getByRole('navigation')).toHaveCount(1);
});

test('the rail marks where the user is, on every screen', async ({page}) => {
  await open(page);
  const rail = sidebar(page);
  for (const name of ['New', 'Runs', 'Machines', 'Usage', 'Settings']) {
    await rail.getByRole('button', {name, exact: true}).click();
    await expect(rail.getByRole('button', {name, exact: true})).toHaveAttribute(
      'aria-current',
      'page',
    );
    await expect(rail.locator('[aria-current="page"]')).toHaveCount(1);
  }
});

test('the Settings menu item opens Settings repeatedly and Back restores the run', async ({
  page,
}) => {
  await openRun(page);
  await openSettingsFromMenu(page);
  await expect(page.getByRole('heading', {name: 'Appearance'})).toBeVisible();
  await openSettingsFromMenu(page);
  await page.getByRole('button', {name: 'Back', exact: true}).click();
  await expect(page.getByRole('heading', {name: 'Appearance'})).toHaveCount(0);
  await expect(page.getByRole('slider', {name: 'Run position'})).toBeVisible();
});

test('Settings sections are peers; Back exits to the same run', async ({page}) => {
  await openRun(page);
  await sidebar(page).getByRole('button', {name: 'Settings'}).click();
  const sections = page.getByRole('navigation', {name: 'Settings sections'});
  const appearance = sections.getByRole('button', {name: 'Appearance'});
  const machines = sections.getByRole('button', {name: /Machines/});
  const notifications = sections.getByRole('button', {name: 'Notifications'});
  await expect(appearance).toHaveAttribute('aria-current', 'page');
  await expect(machines).toBeVisible();
  await expect(notifications).toBeVisible();
  await machines.click();
  await expect(machines).toHaveAttribute('aria-current', 'page');
  await expect(page.getByRole('heading', {name: 'Where runs can go'})).toBeVisible();
  await page.getByRole('button', {name: 'Add a machine'}).click();
  const host = page.getByRole('textbox', {name: 'SSH host'});
  await host.fill('host');
  await host.press(',');
  await expect(host).toHaveValue('host,');
  await openSettingsFromMenu(page);
  await expect(page.getByRole('heading', {name: 'Where runs can go'})).toBeVisible();
  await notifications.click();
  await expect(notifications).toHaveAttribute('aria-current', 'page');
  await expect(page.getByRole('heading', {name: 'Notifications', exact: true})).toBeVisible();
  await appearance.click();
  await expect(page.getByRole('heading', {name: 'Appearance'})).toBeVisible();
  await page.getByRole('button', {name: 'Back', exact: true}).click();
  await expect(page.getByRole('heading', {name: 'Appearance'})).toHaveCount(0);
});

test('Settings section navigation remains reachable at narrow width', async ({page}) => {
  await page.setViewportSize({width: 800, height: 700});
  await openRun(page);
  await sidebar(page).getByRole('button', {name: 'Settings'}).click();
  const sections = page.getByRole('navigation', {name: 'Settings sections'});
  await expect(sections).toBeInViewport();
  await revealNav(page);
  await sections.getByRole('button', {name: 'Notifications'}).click();
  await expect(page.getByRole('heading', {name: 'Notifications', exact: true})).toBeVisible();
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - window.innerWidth,
  );
  expect(overflow).toBeLessThanOrEqual(0);
});

test('nothing sits under the window controls, no control moves the window, and the Run screen fills the window without scrolling the page', async ({
  page,
}) => {
  for (const width of [1440, 1000, 660]) {
    await page.setViewportSize({width, height: 900});
    await openRun(page);
    expect(await screenCheck(page, ['chrome-clear', 'drag-region']), `at ${width} px`).toEqual([]);
    expect(
      await page.evaluate(
        () =>
          document.documentElement.scrollHeight <= window.innerHeight &&
          document.documentElement.scrollWidth <= window.innerWidth,
      ),
    ).toBe(true);
    // The rail's blank space moves the window, between its destinations and its Settings.
    expect(await pressMovesWindow(page, [[40, 450]])).toEqual([true]);
  }
});

test('the page starts at the same height, beside a rail of the same width, on every screen', async ({
  page,
}) => {
  for (const width of [1440, 1000, 660]) {
    await page.setViewportSize({width, height: 900});
    await open(page);
    const place = () =>
      page.locator('.vs-app-content').evaluate(element => {
        const {top, left} = element.getBoundingClientRect();
        return {top, left};
      });
    const home = await place();
    expect(home.top, `the page starts at the window's top at ${width} px`).toBe(0);
    expect(home.left, `the rail is as wide as the corner the lights sit in at ${width} px`).toBe(
      MAC_CHROME.startWidth,
    );
    for (const name of ['New', 'Machines', 'Usage', 'Settings']) {
      await sidebar(page).getByRole('button', {name, exact: true}).click();
      expect(await place(), `${name} at ${width} px`).toEqual(home);
    }
  }
});

test('saved runs that could not be read are said on the dashboard, and the readable ones are still listed', async ({
  page,
}) => {
  const skippedRecent = [
    {index: 16, reason: 'recent.json: runs[16].task must be a task name'},
    {index: 17, reason: 'recent.json: runs[17].instanceId must be a 12-digit instance id'},
  ];
  await open(page, {...SEED, skippedRecent});

  const notice = page.getByRole('main').locator('.vs-banner');
  await expect(notice).toContainText('2 saved runs skipped');
  await expect(notice).toContainText('runs[16].task must be a task name');
  await expect(notice).toContainText('runs[17].instanceId');
  expect(await screenCheck(page)).toEqual([]);
});

test('the rail starts with the app logo, whose tooltip is the version, and it goes home', async ({
  page,
}) => {
  await open(page);
  const mark = sidebar(page).getByRole('button', {name: 'VibeSys'});

  await expect(mark).toHaveAttribute('title', /^VibeSys \S+$/);
  await expect(mark.locator('svg.vs-icon path')).toHaveCount(1);
  await sidebar(page).getByRole('button', {name: 'New', exact: true}).click();
  await mark.click();
  await expect(page.getByRole('heading', {name: 'Runs'})).toBeVisible();
});

interface Layout {
  readonly rail: {readonly top: number; readonly bottom: number; readonly width: number};
  readonly firstItemTop: number;
  readonly page: {readonly top: number; readonly left: number};
  readonly height: number;
}

async function layout(page: Page): Promise<Layout> {
  return page.evaluate(() => {
    const rail = (document.querySelector('.vs-rail') as Element).getBoundingClientRect();
    const first = (document.querySelector('.vs-rail-item') as Element).getBoundingClientRect();
    const content = (document.querySelector('.vs-app-content') as Element).getBoundingClientRect();
    return {
      rail: {top: rail.top, bottom: rail.bottom, width: rail.width},
      firstItemTop: first.top,
      page: {top: content.top, left: content.left},
      height: window.innerHeight,
    };
  });
}

test('on macOS there is no strip: the rail runs the whole window with the lights at its top, and the page starts at the window top', async ({
  page,
}) => {
  await page.setViewportSize({width: 1280, height: 860});
  await open(page, {...SEED, chromeInset: MAC_CHROME});
  const found = await layout(page);

  expect(found.rail).toEqual({top: 0, bottom: found.height, width: MAC_CHROME.startWidth});
  expect(found.page).toEqual({top: 0, left: MAC_CHROME.startWidth});
  // The first destination starts below the corner the lights sit in, never overlapping it.
  expect(found.firstItemTop).toBeGreaterThanOrEqual(MAC_CHROME.startHeight);
  // The corner, the rail's blank space and the top of the page move the window; a destination does not.
  const itemCentre: [number, number] = [MAC_CHROME.startWidth / 2, found.firstItemTop + 20];
  expect(
    await pressMovesWindow(page, [
      [10, 10],
      [MAC_CHROME.startWidth - 10, MAC_CHROME.startHeight - 10],
      [MAC_CHROME.startWidth / 2, found.height / 2],
      [MAC_CHROME.startWidth + 40, 4],
      itemCentre,
    ]),
  ).toEqual([true, true, true, true, false]);
});

test('on Windows and Linux the page starts below the strip the controls cover at the top right, and the rail runs the whole window', async ({
  page,
}) => {
  await page.setViewportSize({width: 1280, height: 860});
  await open(page, {...SEED, chromeInset: WINDOWS_CHROME});
  const found = await layout(page);

  expect(found.rail).toEqual({top: 0, bottom: found.height, width: 64});
  expect(found.page).toEqual({top: WINDOWS_CHROME.endHeight, left: 64});
  expect(
    await pressMovesWindow(page, [
      [64 + 40, 4],
      [64 + 40, WINDOWS_CHROME.endHeight - 4],
    ]),
  ).toEqual([true, true]);
});

test('in full screen the lights are hidden, so the rail and the page take the room back, and windowed again they give it up', async ({
  page,
}) => {
  await page.setViewportSize({width: 1280, height: 860});
  await open(page, {...SEED, chromeInset: MAC_CHROME});
  const windowed = await layout(page);

  await setChromeInset(page, FULL_SCREEN_CHROME);
  const full = await layout(page);
  expect(full.rail.width).toBe(64);
  expect(full.page.left).toBe(64);
  // The destinations move up by exactly the corner the lights left, and the page does not move down.
  expect(windowed.firstItemTop - full.firstItemTop).toBe(MAC_CHROME.startHeight);
  expect(full.page.top).toBe(0);

  await setChromeInset(page, MAC_CHROME);
  expect(await layout(page)).toEqual(windowed);
});
