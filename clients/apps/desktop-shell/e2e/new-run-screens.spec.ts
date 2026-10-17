import {mkdirSync, writeFileSync} from 'node:fs';
import {expect, type Page, test} from '@playwright/test';
import {contactSheetHtml, type SheetEntry} from './checks/contact-sheet.js';
import {open, SEED} from './world.js';

/**
 * The New run page in the states a user meets, in both themes at two window sizes, for the
 * contact sheet the verify-desktop-ui skill's critic step reads. `SCREENS_DIR=<dir>` writes each
 * screenshot (the app window only) and the sheet; without it the states are still reached, so a
 * state that stops being reachable fails here.
 */

const shots = process.env['SCREENS_DIR'] || undefined;
if (shots !== undefined) mkdirSync(shots, {recursive: true});
const sheet: SheetEntry[] = [];

const THEMES = ['paper', 'ink'] as const;
const SIZES = [
  {width: 1000, height: 700},
  {width: 1440, height: 900},
] as const;

const ready = (page: Page) => page.getByRole('region', {name: 'Ready to start'});

interface State {
  readonly name: string;
  readonly reach: (page: Page) => Promise<void>;
}

const STATES: readonly State[] = [
  {
    name: 'ready',
    reach: async page => {
      await expect(page.getByRole('radio', {name: 'claude'})).toBeChecked();
    },
  },
  {
    name: 'checks-open',
    reach: async page => {
      await ready(page).getByRole('button', {name: /Ready/}).click();
      await expect(ready(page)).toContainText('Agent image');
    },
  },
  {
    name: 'agent',
    reach: async page => {
      await page.getByRole('radio', {name: 'codex'}).check();
      await page.getByRole('region', {name: 'Agent'}).scrollIntoViewIfNeeded();
    },
  },
  {
    name: 'more-options',
    reach: async page => {
      await page.getByRole('button', {name: 'More options'}).click();
      await expect(page.getByLabel('Extra arguments')).toBeVisible();
      await page.getByLabel('Extra arguments').scrollIntoViewIfNeeded();
    },
  },
  {
    name: 'failing-check',
    reach: async page => {
      await page.getByRole('radio', {name: 'Open a folder'}).check();
      await page.getByLabel('Folder on host-b').fill('/home/me/vibesys/examples/queue-rs');
      await page.getByRole('button', {name: 'Open', exact: true}).click();
      await expect(ready(page)).toContainText('1 check needs attention');
    },
  },
];

for (const theme of THEMES) {
  for (const size of SIZES) {
    for (const state of STATES) {
      test(`New run, ${state.name}, ${theme} ${size.width}x${size.height}`, async ({page}) => {
        await page.setViewportSize(size);
        await page.emulateMedia({colorScheme: theme === 'ink' ? 'dark' : 'light'});
        await open(page, {
          ...SEED,
          theme,
          appearance: {mode: theme === 'ink' ? 'dark' : 'light', light: 'paper', dark: 'ink'},
        });
        await expect(page.getByRole('heading', {name: 'New run'})).toBeVisible();
        // The checks have answered before the state is reached, as for a user who waited a moment.
        await expect(ready(page)).toContainText('Ready ·');
        await state.reach(page);
        // Start run is in the window in every state.
        await expect(ready(page).getByRole('button', {name: 'Start run'})).toBeInViewport({
          ratio: 1,
        });
        if (shots !== undefined) {
          const file = `new-run__${state.name}__${size.width}x${size.height}__${theme}.png`;
          await page.screenshot({path: `${shots}/${file}`});
          sheet.push({name: `${state.name} ${size.width}x${size.height} ${theme}`, file});
        }
      });
    }
  }
}

test('the contact sheet of the New run states', async ({browser}) => {
  test.skip(shots === undefined, 'set SCREENS_DIR to write the screenshots and the contact sheet');
  if (shots === undefined) return;
  writeFileSync(`${shots}/new-run-sheet.html`, contactSheetHtml(sheet, 4));
  const page = await browser.newPage({viewport: {width: 3200, height: 1200}});
  await page.goto(`file://${shots}/new-run-sheet.html`);
  await page.screenshot({path: `${shots}/new-run-sheet.png`, fullPage: true});
  await page.close();
});
