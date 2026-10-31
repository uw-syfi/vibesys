import {mkdirSync, writeFileSync} from 'node:fs';
import {expect, type Page, test} from '@playwright/test';
import {fixtureNamed} from '@vibesys/run-fixtures';
import {contactSheetHtml, type SheetEntry} from './checks/contact-sheet.js';
import {KnownLedger} from './checks/known-ledger.js';
import {SCREEN_CHECKS, screenCheck} from './checks/screen-check.js';
import {settled} from './checks/settle.js';
import {
  dashboardWorld,
  MANY_RUNS,
  MEASURED_AND_FAILED,
  OMITTED_RUN,
  ONE_RUN,
  open,
  type RunCase,
} from './dashboard-world.js';
import {openFixture, playLive} from './fixture-world.js';

/**
 * The rail and the Runs dashboard, checked against their real layout (`checks/screen-check.ts`) at
 * 1440, 1000 and 660 px: one run, many runs, the runs that need the user alone, a run with
 * more ideas than its chart keeps, machines on their steps to a run list, and the rail on the Run
 * screen and on Settings. `SCREENS_DIR`
 * also writes each screen's screenshot (the app window only) and a contact sheet of them. Each
 * state is rendered once and resized through the widths in one page session.
 */

const WIDTHS = [1440, 1000, 660] as const;
const shots = process.env['SCREENS_DIR'] || undefined;
if (shots !== undefined) mkdirSync(shots, {recursive: true});
const sheet: SheetEntry[] = [];
const ledger = new KnownLedger('dashboard');

const NEEDS_YOU: readonly RunCase[] = MANY_RUNS.filter(run => run.attention !== undefined);

const DASHBOARDS: readonly (readonly [string, readonly RunCase[]])[] = [
  ['one-run', ONE_RUN],
  ['many-runs', MANY_RUNS],
  ['needs-you', NEEDS_YOU],
  ['points-omitted', OMITTED_RUN],
  ['measured-and-failed', MEASURED_AND_FAILED],
];

async function check(page: Page, name: string, ignore: string[] = []): Promise<void> {
  await settled(page);
  const found = (await screenCheck(page, SCREEN_CHECKS)).filter(each => !ignore.includes(each.check));
  const lines = ledger.unexplained(name, found);
  if (shots !== undefined) {
    const file = `${name.replaceAll('/', '__')}.png`;
    await page.screenshot({path: `${shots}/${file}`});
    sheet.push({name, file});
  }
  expect(lines, `screens that break the layout or wording checks:\n${lines.join('\n')}`).toEqual(
    [],
  );
}

for (const [state, runs] of DASHBOARDS) {
  test(`runs dashboard/${state}`, async ({page}) => {
    await page.setViewportSize({width: WIDTHS[0], height: 900});
    await open(page, dashboardWorld(runs));
    for (const width of WIDTHS) {
      await page.setViewportSize({width, height: 900});
      await check(page, `runs/${state}/${width}`);
    }
  });
}

const MACHINE_LOADS: readonly (readonly [string, readonly unknown[]])[] = [
  [
    'one-machine-connecting',
    [{kind: 'unavailable', host: 'local', step: 'gateway', connection: {kind: 'connecting'}}],
  ],
  [
    'machines-at-each-step',
    [
      {kind: 'unavailable', host: 'local', step: 'runs', connection: {kind: 'connecting'}},
      {
        kind: 'unavailable',
        host: 'ssh:host-a',
        step: 'handshake',
        connection: {kind: 'reconnecting', attempt: 2},
      },
      {
        kind: 'unavailable',
        host: 'ssh:host-b',
        step: 'gateway',
        connection: {kind: 'offline', detail: 'gateway startup exited: 1'},
      },
    ],
  ],
];

for (const [state, listings] of MACHINE_LOADS) {
  test(`runs dashboard/${state}`, async ({page}) => {
    await page.setViewportSize({width: WIDTHS[0], height: 900});
    await open(page, {...dashboardWorld([]), listings, recent: []});
    await expect(page.locator('.vs-step-list').first()).toBeVisible();
    for (const width of WIDTHS) {
      await page.setViewportSize({width, height: 900});
      await check(page, `runs/${state}/${width}`);
    }
  });
}

test('runs dashboard/grouped by machine', async ({page}) => {
  await page.setViewportSize({width: WIDTHS[0], height: 900});
  await open(page, dashboardWorld(MANY_RUNS));
  await page.getByRole('combobox', {name: 'Group by'}).selectOption('machine');
  for (const width of WIDTHS) {
    await page.setViewportSize({width, height: 900});
    await check(page, `runs/by-machine/${width}`);
  }
});

for (const width of WIDTHS) {
  test(`the rail on the Run screen/${width}`, async ({page}) => {
    await page.setViewportSize({width, height: 900});
    const fixture = fixtureNamed('queue-rs-live');
    await openFixture(page, fixture);
    await playLive(page, fixture);
    await expect(page.getByRole('slider', {name: 'Run position'})).toBeVisible();
    await check(page, `rail/run/${width}`);
  });

  test(`the rail on Settings/${width}`, async ({page}) => {
    await page.setViewportSize({width, height: 900});
    await open(page, dashboardWorld(ONE_RUN));
    await page
      .getByRole('navigation', {name: 'VibeSys'})
      .getByRole('button', {name: 'Settings'})
      .click();
    await expect(page.getByRole('heading', {name: 'Appearance'})).toBeVisible();
    // The theme swatches are a picker, not a chart: the legend check does not apply.
    await check(page, `rail/settings/${width}`, ['legend']);
  });
}

ledger.requireEveryFailure(() => ledger.screens >= WIDTHS.length * (DASHBOARDS.length + 3));

test('the contact sheet shows every dashboard and rail screen', async ({browser}) => {
  test.skip(shots === undefined, 'set SCREENS_DIR to write the screenshots and the contact sheet');
  if (shots === undefined) return;
  writeFileSync(`${shots}/dashboard-contact-sheet.html`, contactSheetHtml(sheet, 4));
  const page = await browser.newPage({viewport: {width: 2400, height: 1200}});
  await page.goto(`file://${shots}/dashboard-contact-sheet.html`);
  await page.screenshot({path: `${shots}/dashboard-contact-sheet.png`, fullPage: true});
  await page.close();
});
