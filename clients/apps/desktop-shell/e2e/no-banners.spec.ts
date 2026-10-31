/**
 * No screen draws a banner. A run's state is the header's status chip with a few words of why, its
 * actions beside it, and its diagnostic a row of Logs marked on the tab; a problem with one thing
 * is a status word at that thing with the detail on the pointer. Each changed screen is walked in a
 * light and a dark theme, and none has a `.vs-banner`, a full-width strip (tinted or not: the
 * `banner` and `status-row` screen checks), or an "Show details" button on the page (a toast has
 * its own). `NO_BANNERS_DIR=<dir>` writes the app window of each screen and a contact sheet of them.
 */
import {mkdirSync, writeFileSync} from 'node:fs';
import {expect, type Page, test} from '@playwright/test';
import {REQUIRED_HELPER_FEATURES} from '@vibesys/hosts';
import {fixtureNamed} from '@vibesys/run-fixtures';
import {contactSheetHtml, type SheetEntry} from './checks/contact-sheet.js';
import {screenCheck} from './checks/screen-check.js';
import {dashboardWorld, MANY_RUNS, open as openDashboard} from './dashboard-world.js';
import {
  type FakeBridgeSeed,
  installFakeBridge,
  installFakeMachines,
  installFakeNewRun,
} from './fake-bridge.js';
import {openFixture} from './fixture-world.js';
import {addInitScripts, initScript} from './init-scripts.js';
import {NEW_RUN_PAGES, main as newRunMain, openAt as openNewRunAt} from './new-run-flow.js';
import {nav, openSection, openSettingsAt, main as settingsMain} from './settings-machine.js';
import {listed, open, SEED, sidebar} from './world.js';

const shots = process.env['NO_BANNERS_DIR'] || undefined;
if (shots !== undefined) mkdirSync(shots, {recursive: true});
const sheet: SheetEntry[] = [];

const THEMES = [
  {name: 'light', theme: 'paper', mode: 'light'},
  {name: 'dark', theme: 'ink', mode: 'dark'},
] as const;

type Look = (typeof THEMES)[number];

/** `seed` in `look`'s theme. */
const themed = (seed: FakeBridgeSeed, look: Look): FakeBridgeSeed => ({
  ...seed,
  theme: look.theme,
  appearance: {mode: look.mode, light: 'paper', dark: 'ink'},
});

/** The app window only, never the screen. */
async function shoot(page: Page, name: string, look: Look): Promise<void> {
  if (shots === undefined) return;
  const file = `${name}-${look.name}.png`;
  await page.screenshot({path: `${shots}/${file}`});
  sheet.push({name: `${name} · ${look.name}`, file});
}

/**
 * What no screen has: a strip tinted by a status color (the `banner` screen check reads colors, so a
 * banner is found under any name), a row announced as an alert or status or written in a status tone
 * (the `status-row` check), a details button outside a toast, or a status word stretched across the
 * page (a chip is as wide as its label, never half the window).
 */
async function expectNoBanner(page: Page): Promise<void> {
  expect(await screenCheck(page, ['banner', 'status-row'])).toEqual([]);
  const main = page.getByRole('main');
  await expect(main.getByRole('button', {name: 'Show details'})).toHaveCount(0);
  const room = (await main.boundingBox())?.width ?? 0;
  expect(room).toBeGreaterThan(0);
  const widths = await main
    .locator('.vs-chip')
    .evaluateAll(chips => chips.map(chip => chip.getBoundingClientRect().width));
  for (const width of widths) expect(width).toBeLessThan(room / 2);
}

async function failedRun(page: Page, look: Look): Promise<void> {
  const fixture = fixtureNamed('crashed-after-turn');
  await openFixture(page, fixture, {theme: look.theme});
  const main = page.getByRole('main');
  await expectNoBanner(page);
  await expect(main.getByText('Failed', {exact: true}).first()).toBeVisible();
  // A few words of why, beside the status; the whole message is the status's description.
  await expect(main.getByText('Host Git refuses a work tree that may hold a nested')).toBeVisible();
  await expect(
    main.locator('.vs-chip[aria-description]', {hasText: 'Failed'}),
  ).toHaveAccessibleDescription(fixture.end?.reason ?? '');
  await expect(main.getByRole('button', {name: 'Resume', exact: true})).toBeVisible();
  await expect(main.getByRole('button', {name: 'Run again', exact: true})).toBeVisible();
  await expect(main.getByRole('button', {name: 'View logs'})).toHaveCount(0);
  await expect(main.getByRole('img', {name: 'Diagnostic'})).toHaveCount(1);
  expect(await screenCheck(page)).toEqual([]);
  await shoot(page, 'run-failed', look);

  await main.getByRole('radiogroup', {name: 'Show'}).getByText('Logs').click();
  await expect(
    main
      .getByRole('log')
      .getByText(/run failed/i)
      .first(),
  ).toBeVisible();
  await shoot(page, 'run-logs', look);
}

async function newRunMayHaveStarted(page: Page, look: Look): Promise<void> {
  const review = NEW_RUN_PAGES.find(each => each.name === 'review project');
  if (review === undefined) throw new Error('the world has no Review page');
  await openNewRunAt(page, review, themed(SEED, look));
  // The host takes the run and the reply is lost: the call rejects with nothing reported.
  await page.evaluate(() => {
    const bridge = (window as unknown as {vibesysWelcome: Record<string, unknown>}).vibesysWelcome;
    bridge['startRun'] = () => Promise.reject(new Error('the reply was lost'));
  });
  await newRunMain(page).getByRole('button', {name: 'Start run'}).click();
  await expect(newRunMain(page).getByText('May have started', {exact: true})).toBeVisible();
  await expect(newRunMain(page).getByText('Check Runs first', {exact: true})).toBeVisible();
  await expect(newRunMain(page).getByRole('button', {name: 'Open Runs'})).toBeVisible();
  // The state of the checks is a status word beside its heading, not a line of its own.
  await expect(newRunMain(page).locator('.vs-chip', {hasText: /^Ready ·/})).toBeVisible();
  await expectNoBanner(page);
  expect(await screenCheck(page)).toEqual([]);
  await shoot(page, 'new-run-may-have-started', look);
}

async function settingsStatus(page: Page, look: Look): Promise<void> {
  const machines = [
    {host: 'local', label: 'This Mac', checkout: '~/projs/vibesys'},
    {host: 'ssh:host-b', label: 'host-b', checkout: '/home/me/vibesys'},
    {host: 'ssh:host-c', label: 'host-c', checkout: '/home/me/vibesys'},
  ];
  await openSettingsAt(page, 1440, themed({...SEED, machines}, look));
  await openSection(page, 'Agent logins', 'host-c');
  await expect(settingsMain(page).getByText('Needs an update', {exact: true})).toBeVisible();
  await expect(
    settingsMain(page).locator('[aria-description^="The VibeSys on host-c is too old"]'),
  ).toBeAttached();
  await expectNoBanner(page);
  await shoot(page, 'settings-section-old', look);

  await nav(page).getByRole('combobox', {name: 'Machine'}).selectOption({label: 'host-b'});
  const claude = settingsMain(page).locator('.vs-card', {
    has: page.getByRole('heading', {name: 'Claude Code'}),
  });
  await claude.getByRole('button', {name: 'Sign in'}).click();
  await expect(claude.getByText('Login started', {exact: true})).toBeVisible();
  await expect(claude.getByRole('button', {name: 'Check again'})).toBeVisible();
  // The next step is text on the page, not a description a pointer must find.
  await expect(claude.getByText('https://example.com/device', {exact: true})).toBeVisible();
  await expect(claude.getByText('AB12', {exact: true})).toBeVisible();
  await expectNoBanner(page);
  expect(await screenCheck(page)).toEqual([]);
  await shoot(page, 'settings-login-started', look);
}

/**
 * A connection test that failed: a status word with what to read beside it, above the actions that
 * try again, in the form's card and not across the page.
 */
async function clusterTestFailed(page: Page, look: Look): Promise<void> {
  await openSettingsAt(page, 1440, themed(SEED, look));
  await openSection(page, 'Clusters and clouds', 'host-b');
  const settings = settingsMain(page);
  await settings.getByRole('button', {name: 'Add a cluster'}).click();
  await settings.getByRole('textbox', {name: 'Name'}).fill('lab');
  await settings.getByRole('textbox', {name: 'Workspace on the cluster'}).fill('/scratch/vibesys');
  await settings.getByRole('textbox', {name: 'SSH host alias'}).fill('no-workspace');
  await settings.getByRole('button', {name: 'Test connection'}).click();
  const failure = settings.getByRole('alert');
  await expect(failure.locator('.vs-chip', {hasText: 'The workspace is not usable'})).toBeVisible();
  await expectNoBanner(page);
  expect(await screenCheck(page)).toEqual([]);
  await shoot(page, 'settings-cluster-test-failed', look);
}

async function machinesStatus(page: Page, look: Look): Promise<void> {
  const seed = themed(
    {
      ...SEED,
      listings: [],
      recent: [],
      machines: [
        {host: 'local', label: 'This Mac', checkout: '~/projs/vibesys'},
        {host: 'ssh:host-a', label: 'host-a', checkout: '/data/vibesys'},
        {host: 'ssh:host-b', label: 'host-b', checkout: '~/vibesys'},
      ],
      reports: {
        'ssh:host-a': {kind: 'sign-in', detail: 'your SSH key needs its passphrase.'},
        'ssh:host-b': {
          kind: 'checkout-not-found',
          detail: '~/vibesys on host-b is not a directory.',
        },
      },
    },
    look,
  );
  await addInitScripts(page, [
    initScript(installFakeBridge, seed),
    initScript(installFakeMachines, seed),
    initScript(installFakeNewRun, seed),
  ]);
  await page.goto('./shell.html');
  await sidebar(page).getByRole('button', {name: 'Settings'}).click();
  await page
    .getByRole('navigation', {name: 'Settings sections'})
    .getByRole('button', {name: /Machines/})
    .click();
  const list = page.getByRole('tree', {name: 'Machines'});
  await list.getByRole('treeitem', {name: 'host-a'}).click();
  await expect(page.getByRole('button', {name: 'Sign in', exact: true})).toBeVisible();
  await expect(
    page
      .getByRole('main')
      .locator('[aria-description="The app couldn’t connect: your SSH key needs its passphrase."]'),
  ).toBeAttached();
  await expectNoBanner(page);
  await shoot(page, 'machines-sign-in', look);
  await list.getByRole('treeitem', {name: 'host-b'}).click();
  await expect(page.getByRole('combobox', {name: 'VibeSys checkout on host-b'})).toBeVisible();
  await expectNoBanner(page);
  expect(await screenCheck(page)).toEqual([]);
  await shoot(page, 'machines-checkout', look);
}

async function newRunWhere(page: Page, look: Look): Promise<void> {
  const newRun = SEED.newRun;
  const where = NEW_RUN_PAGES.find(each => each.name === 'where');
  if (newRun === undefined || where === undefined) throw new Error('the world has no Where page');
  const features = REQUIRED_HELPER_FEATURES.filter(feature => feature !== 'run_trace');
  await openNewRunAt(page, where, themed({...SEED, newRun: {...newRun, features}}, look));
  await expect(newRunMain(page).getByText('Needs an update', {exact: true})).toBeVisible();
  await expectNoBanner(page);
  await shoot(page, 'new-run-where', look);
}

/**
 * A machine that listed some of its projects and could not list the rest: the Runs title carries the
 * status word, the runs that were read are listed under it, and no row of its own says the list is
 * incomplete.
 */
async function runsIncomplete(page: Page, look: Look): Promise<void> {
  const world = dashboardWorld(MANY_RUNS);
  const listed = Object.fromEntries(
    Object.entries(world.projectRuns ?? {}).filter(([key]) => !key.startsWith('ssh:host-b|')),
  );
  await openDashboard(page, themed({...world, projectRuns: listed}, look));
  const main = page.getByRole('main');
  await expect(main.locator('.vs-chip', {hasText: 'Run list incomplete'})).toBeVisible();
  // The runs that were read are there, and so is the one machine's run its registry reports.
  await expect(main.locator('.vs-overview-tile', {hasText: 'host-a'}).first()).toBeVisible();
  await expectNoBanner(page);
  expect(await screenCheck(page)).toEqual([]);
  await shoot(page, 'runs-incomplete', look);
}

async function runsSkipped(page: Page, look: Look): Promise<void> {
  const skippedRecent = [
    {index: 16, reason: 'recent.json: runs[16].task must be a task name'},
    {index: 17, reason: 'recent.json: runs[17].instanceId must be a 12-digit instance id'},
  ];
  await open(page, themed({...SEED, skippedRecent, listings: [listed([], [])]}, look));
  await expect(
    page.getByRole('main').getByText('2 saved runs skipped', {exact: true}),
  ).toBeVisible();
  await expectNoBanner(page);
  await shoot(page, 'runs-skipped', look);
}

const SCREENS: readonly {
  readonly title: string;
  readonly walk: (page: Page, look: Look) => Promise<void>;
}[] = [
  {title: 'a failed run: its state in the header, its diagnostic in Logs', walk: failedRun},
  {title: 'Settings: a section too old and a login started are status words', walk: settingsStatus},
  {
    title: 'New run: a start that may have happened says so on the page',
    walk: newRunMayHaveStarted,
  },
  {
    title: 'Settings: a failed connection test is a status word in the form',
    walk: clusterTestFailed,
  },
  {
    title: 'Machines: the status word, its reason on the pointer, its fix beside it',
    walk: machinesStatus,
  },
  {title: 'New run: a machine too old is a status word on Where', walk: newRunWhere},
  {
    title: 'Runs: saved runs that could not be read are a status word by the title',
    walk: runsSkipped,
  },
  {
    title: 'Runs: a project whose runs could not be listed is a status word by the title',
    walk: runsIncomplete,
  },
];

for (const look of THEMES) {
  test.describe(`${look.name} theme`, () => {
    test.use({colorScheme: look.mode, viewport: {width: 1440, height: 900}});
    for (const screen of SCREENS) {
      test(screen.title, ({page}) => screen.walk(page, look));
    }
  });
}

test('the contact sheet of the screens that had a banner', async ({browser}) => {
  test.skip(
    shots === undefined,
    'set NO_BANNERS_DIR to write the screenshots and the contact sheet',
  );
  if (shots === undefined) return;
  writeFileSync(`${shots}/contact-sheet.html`, contactSheetHtml(sheet, 4));
  const page = await browser.newPage({viewport: {width: 3000, height: 1200}});
  await page.goto(`file://${shots}/contact-sheet.html`);
  await page.screenshot({path: `${shots}/contact-sheet.png`, fullPage: true});
  await page.close();
});
