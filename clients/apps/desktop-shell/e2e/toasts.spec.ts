/**
 * Toasts and compact banners in the window: a failure passed through is a toast at the top right,
 * collapsed, a small gap below the screen's header row on every screen (at the page's top edge where
 * a screen has none) and clear of the rail and the window's own controls; a state the screen holds
 * is one line. `TOASTS_SCREENSHOTS` names a directory for the window screenshots of the app check.
 */
import {mkdirSync, writeFileSync} from 'node:fs';
import {join} from 'node:path';
import AxeBuilder from '@axe-core/playwright';
import {expect, type Page, test} from '@playwright/test';
import type {ChromeInset} from '@vibesys/ipc';
import {contactSheetHtml, type SheetEntry} from './checks/contact-sheet.js';
import {settled} from './checks/settle.js';
import {
  type FakeBridgeSeed,
  installFakeBridge,
  installFakeChrome,
  installFakeMachineSettings,
  installFakeMachines,
  installFakeNewRun,
  installFakeProjectRuns,
  installFakeUsage,
  MAC_CHROME,
  WINDOWS_CHROME,
} from './fake-bridge.js';
import {addInitScripts, initScript} from './init-scripts.js';
import {journeyScript} from './journey-world.js';
import {installGatewayOnly} from './retired-run-bridge.js';
import {sidebar, SEED as WORLD} from './world.js';

const SHOTS = process.env['TOASTS_SCREENSHOTS'];

const SEED: FakeBridgeSeed = {
  theme: 'ink',
  chromeInset: MAC_CHROME,
  appearance: {mode: 'dark', light: 'paper', dark: 'ink'},
  listings: [],
  recent: [],
  machines: [
    {host: 'local', label: 'This Mac', checkout: '~/projs/vibesys'},
    {host: 'ssh:host-b', label: 'host-b', checkout: '~/vibesys'},
  ],
  reports: {'ssh:host-b': {kind: 'needs-update', facts: null}},
  facts: {
    checkout: {root: '/home/me/vibesys', version: '0.1.1', commit: 'e66d941', dirty: false},
    hardware: {cpu_cores: 64, gpus: [], accelerator: 'cpu'},
    docker: {reachable: true, version: '29.1'},
    harness_clis: ['claude'],
    harness_cli_names: {claude: 'Claude Code'},
    runs: {live: 0, ended_today: 0},
  },
};

/** The machines list the main process refused: a failure the person passes through. */
const refuseMachines = initScript(() => {
  const bridge = (window as unknown as {vibesysWelcome: Record<string, unknown>}).vibesysWelcome;
  bridge['machines'] = () =>
    Promise.resolve({
      ok: false,
      error: 'Could not read /var/folders/zz/T/vibesys-ab12/hosts.json: unreadable.',
      authNeeded: false,
    });
}, undefined);

async function open(
  page: Page,
  width: number,
  height = 700,
  refused = true,
  chromeInset: ChromeInset = SEED.chromeInset,
): Promise<void> {
  const seed = {...SEED, chromeInset};
  await page.setViewportSize({width, height});
  await addInitScripts(page, [
    initScript(installFakeChrome, seed),
    initScript(installFakeBridge, seed),
    initScript(installFakeMachines, seed),
    initScript(installFakeNewRun, seed),
    ...(refused ? [refuseMachines] : []),
  ]);
  await page.goto('./shell.html');
}

const sheet: SheetEntry[] = [];

/** The app window only, never the screen: the page is the window's content. */
async function shoot(page: Page, name: string): Promise<void> {
  if (SHOTS === undefined) return;
  mkdirSync(SHOTS, {recursive: true});
  await page.screenshot({path: join(SHOTS, `${name}.png`)});
  sheet.push({name, file: `${name}.png`});
}

const toast = (page: Page) => page.getByRole('alert');
/** The window's own stack of toasts: a screen can hold alerts of its own. */
const stacked = (page: Page) =>
  page.getByRole('region', {name: 'Notifications'}).getByRole('alert');

const CHROMES = [
  {name: 'macOS', chrome: MAC_CHROME},
  {name: 'Windows', chrome: WINDOWS_CHROME},
] as const;

/** The smallest width the app's window can be (`minWidth` of the main window). */
const MIN_WIDTH = 960;

interface Box {
  readonly x: number;
  readonly y: number;
  readonly width: number;
  readonly height: number;
}

const overlaps = (a: Box, b: Box): boolean =>
  a.x < b.x + b.width && b.x < a.x + a.width && a.y < b.y + b.height && b.y < a.y + a.height;

/** The window's header rows and every box drawn in the first of them, as the layout has them now. */
interface HeaderFacts {
  readonly rows: number;
  readonly header: Box | null;
  readonly held: readonly Box[];
}

/** A page's header (`.vs-page-header`) or a screen's (`.vs-screen-header`): the window's one header row. */
async function headerFacts(page: Page): Promise<HeaderFacts> {
  return page.evaluate(() => {
    const boxOf = (element: Element): Box => {
      const {x, y, width, height} = element.getBoundingClientRect();
      return {x, y, width, height};
    };
    const rows = [...document.querySelectorAll('.vs-page-header, .vs-screen-header')];
    const [first] = rows;
    if (first === undefined) return {rows: 0, header: null, held: []};
    const held = [...first.querySelectorAll('*')]
      .map(boxOf)
      .filter(box => box.width > 0 && box.height > 0);
    return {rows: rows.length, header: boxOf(first), held};
  });
}

/** What a person presses to reach a screen of the window, and whether it has a header row. */
interface Screen {
  readonly name: string;
  readonly header: boolean;
  readonly reach: (page: Page) => Promise<void>;
}

const rail = (page: Page, name: string | RegExp, exact = false) =>
  sidebar(page).getByRole('button', {name, exact});
const heading = (page: Page, name: string) => page.getByRole('heading', {name, level: 1});

const RUNS: Screen = {
  name: 'Runs',
  header: true,
  reach: async page => {
    await rail(page, /^Runs/).click();
    await expect(heading(page, 'Runs')).toBeVisible();
  },
};
const RUN_PAGE: Screen = {
  name: 'Run page',
  header: true,
  reach: async page => {
    await RUNS.reach(page);
    await page
      .getByRole('main')
      .getByRole('button', {name: /^queue-rs · mpmc/})
      .click();
    await expect(page.getByRole('slider', {name: 'Run position'})).toBeVisible();
  },
};
/** New run has no header row: its heading is the first thing its form says. */
const SCREENS: readonly Screen[] = [
  RUNS,
  {
    name: 'New run',
    header: false,
    reach: async page => {
      await rail(page, 'New', true).click();
      await expect(page.getByLabel('New run steps')).toBeVisible();
    },
  },
  {
    name: 'Machines',
    header: true,
    reach: async page => {
      await rail(page, 'Machines').click();
      await expect(heading(page, 'Machines')).toBeVisible();
    },
  },
  {
    name: 'Usage',
    header: true,
    reach: async page => {
      await rail(page, 'Usage').click();
      await expect(heading(page, 'Usage')).toBeVisible();
    },
  },
  {
    name: 'Settings',
    header: true,
    reach: async page => {
      await rail(page, 'Settings').click();
      await expect(page.getByRole('navigation', {name: 'Settings sections'})).toBeVisible();
    },
  },
  RUN_PAGE,
];

/**
 * The toast on the screen the window shows now: at the top right, inside the window and clear of the
 * rail and of the window's own controls (the traffic lights, the strip Windows draws), and a small
 * gap below the header row without covering anything it holds; with no header row, at the page's top
 * edge. Soft, so one run names every screen that fails.
 */
async function expectClear(
  page: Page,
  screen: Pick<Screen, 'name' | 'header'>,
  chrome: ChromeInset,
  width: number,
): Promise<void> {
  const box = await stacked(page).boundingBox();
  const side = await sidebar(page).boundingBox();
  if (box === null || side === null)
    throw new Error(`${screen.name}: the toast and rail are drawn`);
  const facts = await headerFacts(page);
  const at = (what: string): string => `${screen.name} at ${width}px: ${what}`;
  expect.soft(box.x + box.width, at('inside the window')).toBeLessThanOrEqual(width);
  expect.soft(box.x + box.width, at('against the right edge')).toBeGreaterThan(width - 40);
  expect.soft(overlaps(box, side), at('clear of the rail')).toBe(false);
  const lights = {x: 0, y: 0, width: chrome.startWidth, height: chrome.startHeight};
  expect.soft(overlaps(box, lights), at('clear of the traffic lights')).toBe(false);
  expect.soft(box.y, at("below the window's end strip")).toBeGreaterThanOrEqual(chrome.endHeight);
  expect.soft(facts.rows, at('header rows')).toBe(screen.header ? 1 : 0);
  if (facts.header === null) {
    expect.soft(box.y - chrome.endHeight, at("at the page's top edge")).toBeLessThan(16);
    return;
  }
  const bottom = facts.header.y + facts.header.height;
  expect.soft(box.y, at("below the header row's bottom")).toBeGreaterThanOrEqual(bottom);
  expect.soft(box.y - bottom, at('a small gap below it')).toBeLessThan(16);
  expect.soft(facts.held.length, at('the header row holds something')).toBeGreaterThan(0);
  expect
    .soft(
      facts.held.filter(held => overlaps(box, held)),
      at('over no box in the header row'),
    )
    .toEqual([]);
}

/** The shell over a world with runs, machines and a live run, whose machines list the host refuses. */
async function openScreens(page: Page, width: number, chrome: ChromeInset): Promise<void> {
  const seed: FakeBridgeSeed = {
    ...WORLD,
    theme: 'ink',
    appearance: SEED.appearance,
    chromeInset: chrome,
  };
  await page.setViewportSize({width, height: 800});
  await addInitScripts(page, [
    initScript(installFakeChrome, seed),
    initScript(installFakeBridge, seed),
    initScript(installFakeMachines, seed),
    initScript(installFakeMachineSettings, seed),
    initScript(installFakeNewRun, seed),
    initScript(installFakeProjectRuns, seed),
    initScript(installFakeUsage, seed),
    initScript(installGatewayOnly, undefined),
    journeyScript('live'),
    refuseMachines,
  ]);
  await page.goto('./shell.html');
  await expect(stacked(page)).toHaveCount(1);
}

for (const {name, chrome} of CHROMES) {
  for (const width of [1440, MIN_WIDTH]) {
    test(`the toast is below the header row, clear of its controls, on every screen at ${width}px on ${name}`, async ({
      page,
    }) => {
      await openScreens(page, width, chrome);
      for (const screen of SCREENS) {
        await screen.reach(page);
        await settled(page);
        await expectClear(page, screen, chrome, width);
        await shoot(
          page,
          `toast-${screen.name.toLowerCase().replace(/\W+/g, '-')}-${name}-${width}`,
        );
      }
    });
  }
}

test('a taller header is still cleared: the toast keeps its gap below the header, whatever its height', async ({
  page,
}) => {
  await openScreens(page, 1440, MAC_CHROME);
  // A page's header (Runs) and a screen's (the run page), each grown by more than it holds.
  for (const screen of [RUNS, RUN_PAGE]) {
    await screen.reach(page);
    const gaps: number[] = [];
    for (const extra of [0, 37, 120]) {
      await page.evaluate(grown => {
        const row = document.querySelector('.vs-page-header, .vs-screen-header');
        if (!(row instanceof HTMLElement)) throw new Error('the screen has a header row');
        row.style.paddingBottom = `${grown}px`;
      }, extra);
      await settled(page);
      const box = await stacked(page).boundingBox();
      const {header} = await headerFacts(page);
      if (box === null || header === null) throw new Error('the toast and the header are drawn');
      expect(box.y, `${screen.name} grown by ${extra}px`).toBeGreaterThanOrEqual(
        header.y + header.height,
      );
      gaps.push(Math.round(box.y - (header.y + header.height)));
    }
    expect(gaps, `${screen.name}: the gap below the header at each height`).toEqual(
      gaps.map(() => gaps[0]),
    );
  }
});

for (const {name, chrome} of CHROMES) {
  for (const width of [1440, MIN_WIDTH]) {
    test(`a failure passed through is a small collapsed toast at the top right at ${width}px on ${name}`, async ({
      page,
    }) => {
      await open(page, width, 700, true, chrome);
      await expect(toast(page)).toHaveCount(1);
      await expect(toast(page)).toContainText('The machines could not be read');
      await expect(page.getByText(/unreadable/)).toHaveCount(0);
      const box = await toast(page).boundingBox();
      if (box === null) throw new Error('the toast is drawn');
      // The first screen is Runs: the toast is clear of its header row, wherever the layout puts it.
      await expectClear(page, {name: 'The first screen', header: true}, chrome, width);
      expect(box.height).toBeLessThan(80);
      await shoot(page, `toast-collapsed-${name}-${width}`);
      await page.getByRole('button', {name: 'Show details'}).click();
      // The refusal named a temporary folder; the toast shows the file's name only.
      await expect(page.getByText('Could not read hosts.json: unreadable.')).toBeVisible();
      await expect(page.getByText(/\/var\/folders/)).toHaveCount(0);
      await shoot(page, `toast-open-${name}-${width}`);
      const axe = await new AxeBuilder({page}).analyze();
      expect(axe.violations.map(violation => `${violation.id}: ${violation.help}`)).toEqual([]);
      await page.getByRole('button', {name: 'Dismiss'}).click();
      await expect(toast(page)).toHaveCount(0);
    });
  }
}

test('the toast is reachable and operable from the keyboard', async ({page}) => {
  await open(page, 1440);
  const expand = page.getByRole('button', {name: 'Show details'});
  await expect(expand).toBeVisible();
  await expand.focus();
  await expect(expand).toBeFocused();
  await page.keyboard.press('Enter');
  await expect(page.getByText('Could not read hosts.json: unreadable.')).toBeVisible();
  await page.keyboard.press('Tab');
  await expect(page.getByRole('button', {name: 'Dismiss'})).toBeFocused();
  await page.keyboard.press('Enter');
  await expect(toast(page)).toHaveCount(0);
});

for (const width of [1440, 660]) {
  test(`a state the screen holds is one line with its detail behind a button at ${width}px`, async ({
    page,
  }) => {
    await open(page, 1440, 900, false);
    await page
      .getByRole('navigation', {name: 'VibeSys'})
      .getByRole('button', {name: 'Settings'})
      .click();
    await page
      .getByRole('navigation', {name: 'Settings sections'})
      .getByRole('button', {name: /Machines/})
      .click();
    await page.getByRole('treeitem', {name: 'host-b'}).click();
    await page.setViewportSize({width, height: 900});
    const banner = page.locator('.vs-banner').filter({hasText: 'Needs an update'});
    await expect(banner).toBeVisible();
    const box = await banner.boundingBox();
    if (box === null) throw new Error('the banner is drawn');
    expect(box.height).toBeLessThan(56);
    await expect(page.getByText('VibeSys on host-b needs an update')).toHaveCount(0);
    await shoot(page, `banner-collapsed-${width}`);
    await banner.getByRole('button', {name: 'Show details'}).click();
    await expect(page.getByText('VibeSys on host-b needs an update')).toBeVisible();
    await shoot(page, `banner-open-${width}`);
    await expect(toast(page)).toHaveCount(0);
  });
}

test('toasts and banners app-window contact sheet', async ({page}) => {
  if (SHOTS === undefined || sheet.length === 0) return;
  writeFileSync(join(SHOTS, 'toasts-contact-sheet.html'), contactSheetHtml(sheet, 2));
  await page.goto(`file://${join(SHOTS, 'toasts-contact-sheet.html')}`);
  await page.setViewportSize({width: 1500, height: 900});
  await page.screenshot({path: join(SHOTS, 'toasts-contact-sheet.png'), fullPage: true});
});
