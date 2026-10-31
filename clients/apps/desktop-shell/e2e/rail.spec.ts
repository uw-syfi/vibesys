import {expect, type Page, test} from '@playwright/test';
import {screenCheck} from './checks/screen-check.js';
import {
  type FakeBridgeSeed,
  installFakeBridge,
  installFakeMachines,
  installFakeProjectRuns,
} from './fake-bridge.js';
import {addInitScripts, initScript} from './init-scripts.js';
import {journeyScript} from './journey-world.js';
import {revealNav} from './settings-machine.js';
import {STARTED_AT} from './started-at.js';
import {drawnUnderWindowControls, type FakeHostWindow, open, SEED, sidebar} from './world.js';

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

test('nothing sits under the window controls, the rail drags the window, and the Run screen fills the window without scrolling the page', async ({
  page,
}) => {
  for (const width of [1440, 1000, 660]) {
    await page.setViewportSize({width, height: 900});
    await openRun(page);
    expect(await drawnUnderWindowControls(page), `at ${width} px`).toEqual([]);
    expect(
      await page.evaluate(
        () =>
          document.documentElement.scrollHeight <= window.innerHeight &&
          document.documentElement.scrollWidth <= window.innerWidth,
      ),
    ).toBe(true);
    const drag = await sidebar(page).evaluate(element =>
      getComputedStyle(element).getPropertyValue('-webkit-app-region'),
    );
    expect(drag).toBe('drag');
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
    expect(home.left, `the rail is 64 px wide at ${width} px`).toBe(64);
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

  const notice = page.getByRole('main').getByText('2 saved runs skipped', {exact: true});
  await expect(notice).toBeVisible();
  // What was skipped is the notice's description.
  await expect(notice).toHaveAccessibleDescription(/runs\[16\]\.task must be a task name/);
  await expect(notice).toHaveAccessibleDescription(/runs\[17\]\.instanceId/);
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

test('a band across the top is as tall as the window controls, and the rail and the page start below it', async ({
  page,
}) => {
  await page.setViewportSize({width: 1280, height: 860});
  await open(page);
  const inset = await page.evaluate(() =>
    Number.parseFloat(
      getComputedStyle(document.documentElement).getPropertyValue('--chrome-inset-height'),
    ),
  );
  const edges = await page.evaluate(() => {
    const top = (selector: string): number =>
      document.querySelector(selector)?.getBoundingClientRect().top ?? Number.NaN;
    const band = document.querySelector('.vs-rail-band')?.getBoundingClientRect();
    return {
      bandBottom: band?.bottom ?? Number.NaN,
      bandTop: band?.top ?? Number.NaN,
      rail: top('.vs-rail'),
      page: top('.vs-app-content'),
      drag: getComputedStyle(document.querySelector('.vs-rail-band') as Element).getPropertyValue(
        '-webkit-app-region',
      ),
    };
  });

  expect(edges.bandTop).toBe(0);
  expect(edges.bandBottom).toBeGreaterThanOrEqual(inset);
  expect(edges.rail).toBe(edges.bandBottom);
  expect(edges.page).toBe(edges.bandBottom);
  expect(edges.drag).toBe('drag');
});
