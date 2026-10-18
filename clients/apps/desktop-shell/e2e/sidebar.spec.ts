import {expect, type Page, test} from '@playwright/test';
import {type FakeBridgeSeed, installFakeBridge, installFakeMachines} from './fake-bridge.js';
import {installFakeRun} from './fake-run.js';
import {type FakeServerSeed, installFakeServer} from './fake-server.js';
import {judgeEvents, RUN_ID, traceEvents, turnEvents} from './fake-trace.js';
import {type FakeHostWindow, SEED, sidebar} from './world.js';

/**
 * Hiding the sidebar completely: the toggle in the sidebar's header, the same control at the top of
 * the main area beside the window controls when it is hidden, the keyboard shortcut (the Fake
 * bridge is a Mac's, so Command and backslash) and the application menu's item. A hidden sidebar
 * takes no width and is out of the accessibility tree and the tab order, the Run screen fills the
 * window without scrolling the page, and the choice is kept for the next start. `SIDEBAR_SHOTS`
 * names a directory to save the window's screenshots in.
 */

const PROJECT = '/home/me/vibesys-projects/queue-rs';
const INSTANCE = '00000000000a';
const EVENTS = (() => {
  const spans = traceEvents();
  const turns = turnEvents(spans.length);
  return [...spans, ...turns, ...judgeEvents(spans.length + turns.length)];
})();

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
          startedAt: 1,
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

const SERVER: FakeServerSeed = {
  runId: RUN_ID,
  batch: {
    type: 'event_batch',
    events: EVENTS,
    through_sequence: EVENTS.length,
    store_id: 'store-1',
    history_after_sequence: 0,
  },
  snapshot: {run_id: RUN_ID, sequence: 0, status: 'running'},
};

const shots = process.env['SIDEBAR_SHOTS'];

async function shot(page: Page, name: string): Promise<void> {
  if (shots === undefined) return;
  const width = page.viewportSize()?.width ?? 0;
  await page.screenshot({path: `${shots}/${name}-${width}.png`});
}

async function openRun(page: Page): Promise<void> {
  await page.addInitScript(installFakeBridge, WORLD);
  await page.addInitScript(installFakeMachines, WORLD);
  await page.addInitScript(installFakeRun, {streams: [], text: {}});
  await page.addInitScript(installFakeServer, SERVER);
  await page.goto('./shell.html');
  await sidebar(page)
    .getByRole('button', {name: /^\S+ queue-rs · mpmc/})
    .click();
  await expect(page.getByRole('main')).toBeVisible();
}

const hide = (page: Page) => page.getByRole('button', {name: 'Hide sidebar'});
const show = (page: Page) => page.getByRole('button', {name: 'Show sidebar'});
const mainWidth = (page: Page) =>
  page.locator('.vs-app-main').evaluate(element => element.getBoundingClientRect().width);

test.use({colorScheme: 'dark', viewport: {width: 1440, height: 900}});

test('the button hides the sidebar completely and shows it again', async ({page}) => {
  await openRun(page);
  const shown = await mainWidth(page);
  await expect(hide(page)).toHaveAttribute('title', 'Hide sidebar (⌘\\)');
  await expect(hide(page)).toHaveText('');
  await shot(page, 'shown');

  await hide(page).click();
  await expect(sidebar(page)).toHaveCount(0);
  await expect(page.getByRole('button', {name: 'New run'})).toHaveCount(0);
  await expect(show(page)).toHaveAttribute('title', 'Show sidebar (⌘\\)');
  expect(await mainWidth(page)).toBe(1440);
  expect(await mainWidth(page)).toBeGreaterThan(shown);
  await shot(page, 'hidden');

  await show(page).click();
  await expect(sidebar(page)).toBeVisible();
  await expect(hide(page)).toBeVisible();
  expect(await mainWidth(page)).toBe(shown);
});

test('the shortcut and the menu item toggle it, from any focus', async ({page}) => {
  await openRun(page);
  await page.keyboard.press('Meta+Backslash');
  await expect(sidebar(page)).toHaveCount(0);
  await page.keyboard.press('Meta+Backslash');
  await expect(sidebar(page)).toBeVisible();
  // Another chord does nothing, and Control alone is not this Mac's shortcut.
  await page.keyboard.press('Control+Backslash');
  await page.keyboard.press('Meta+b');
  await expect(sidebar(page)).toBeVisible();

  await page.evaluate(() => (window as unknown as FakeHostWindow).fakeHost.toggleSidebar());
  await expect(sidebar(page)).toHaveCount(0);
  await page.evaluate(() => (window as unknown as FakeHostWindow).fakeHost.toggleSidebar());
  await expect(sidebar(page)).toBeVisible();
});

test('hidden, no sidebar control can be focused, and the run still opens from the shortcut', async ({
  page,
}) => {
  await openRun(page);
  await page.keyboard.press('Meta+Backslash');
  await expect(sidebar(page)).toHaveCount(0);
  const reached = new Set<string>();
  for (let press = 0; press < 40; press += 1) {
    await page.keyboard.press('Tab');
    reached.add(
      await page.evaluate(() =>
        document.activeElement?.closest('.vs-app-nav') === null ? 'main' : 'sidebar',
      ),
    );
  }
  expect([...reached]).toEqual(['main']);
  expect(await page.locator('.vs-app-nav button:visible').count()).toBe(0);
  // Showing it again brings back New run and the run switcher: no second navigation exists.
  await page.keyboard.press('Meta+Backslash');
  await expect(sidebar(page).getByRole('button', {name: 'New run'})).toBeVisible();
  await expect(sidebar(page).getByRole('button', {name: /^\S+ queue-rs · mpmc/})).toBeVisible();
});

test('the choice is kept for the next start', async ({page}) => {
  await openRun(page);
  await hide(page).click();
  await expect(sidebar(page)).toHaveCount(0);
  await page.reload();
  await expect(show(page)).toBeVisible();
  await expect(sidebar(page)).toHaveCount(0);
  await show(page).click();
  await page.reload();
  await expect(sidebar(page)).toBeVisible();
});

test('hidden, nothing sits under the window controls, and the Run screen fills the window without scrolling the page', async ({
  page,
}) => {
  for (const width of [1440, 1000]) {
    await page.setViewportSize({width, height: 900});
    await openRun(page);
    await page.keyboard.press('Meta+Backslash');
    await expect(sidebar(page)).toHaveCount(0);
    const overlaps = await page.evaluate(() => {
      const style = getComputedStyle(document.documentElement);
      const inset = {
        width: Number.parseFloat(style.getPropertyValue('--chrome-inset-width')),
        height: Number.parseFloat(style.getPropertyValue('--chrome-inset-height')),
      };
      const drawn = [...document.querySelectorAll('body *')].filter(
        element =>
          element.matches('a, button, input, select, textarea, [tabindex]:not([tabindex="-1"])') ||
          [...element.childNodes].some(
            node => node.nodeType === Node.TEXT_NODE && node.textContent?.trim() !== '',
          ),
      );
      return drawn.flatMap(element => {
        const box = element.getBoundingClientRect();
        const hit = box.width > 0 && box.left < inset.width && box.top < inset.height;
        return hit ? [`${element.textContent?.trim()} at ${box.left},${box.top}`] : [];
      });
    });
    expect(overlaps, `at ${width} px`).toEqual([]);
    const box = await show(page).boundingBox();
    expect(box?.x ?? 0).toBeGreaterThanOrEqual(84);
    expect(
      await page.evaluate(
        () =>
          document.documentElement.scrollHeight <= window.innerHeight &&
          document.documentElement.scrollWidth <= window.innerWidth,
      ),
    ).toBe(true);
    const drag = await page
      .locator('.vs-app-strip')
      .evaluate(element => getComputedStyle(element).getPropertyValue('-webkit-app-region'));
    expect(drag).toBe('drag');
    await shot(page, 'hidden');
    await page.keyboard.press('Meta+Backslash');
    await expect(sidebar(page)).toBeVisible();
    await shot(page, 'shown');
  }
});
