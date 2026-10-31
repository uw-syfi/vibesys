import {mkdirSync, writeFileSync} from 'node:fs';
import {expect, test} from '@playwright/test';
import {contactSheetHtml, type SheetEntry} from './checks/contact-sheet.js';
import {KnownLedger} from './checks/known-ledger.js';
import {EVERY_CHECK, screenCheck} from './checks/screen-check.js';
import {main, NEW_RUN_PAGES, openAt} from './new-run-flow.js';
import {SEED} from './world.js';

/**
 * Every page of the guided New run, at the three window widths the app is used at: the layout
 * checks (controls in a row align, columns start at one top, nothing overflows or overlaps, and
 * the rest) hold on each. The width is the page's own `innerWidth`, read back, never assumed.
 * `SCREENS_DIR=<dir>` writes each page's screenshot (the page only) and a contact sheet.
 */

const shots = process.env['SCREENS_DIR'] || undefined;
if (shots !== undefined) mkdirSync(shots, {recursive: true});
const sheet: SheetEntry[] = [];
const ledger = new KnownLedger('new-run');

const WIDTHS = [1440, 1000, 660] as const;
const THEMES = ['paper', 'ink'] as const;

for (const theme of THEMES) {
  for (const width of WIDTHS) {
    // The dark theme is looked at once, at the widest window: its colors do not change layout.
    if (theme === 'ink' && width !== 1440) continue;
    for (const each of NEW_RUN_PAGES) {
      test(`${each.name} at ${width}px (${theme}) passes the layout checks`, async ({page}) => {
        await page.setViewportSize({width, height: 900});
        await page.emulateMedia({colorScheme: theme === 'ink' ? 'dark' : 'light'});
        await openAt(page, each, {
          ...SEED,
          theme,
          appearance: {mode: theme === 'ink' ? 'dark' : 'light', light: 'paper', dark: 'ink'},
        });
        expect(await page.evaluate(() => window.innerWidth), 'the window is as wide as asked').toBe(
          width,
        );
        await expect(main(page).getByRole('heading', {level: 1})).toBeVisible();
        // The roles come with the first check of the run's inputs; the page is checked with them.
        if (each.agent !== undefined && each.step === 'agent') {
          await expect(main(page).getByLabel('Judge model')).toBeVisible();
        }
        // The goal of the user's own task is read from the machine; the page is checked with it.
        if (each.step === 'goal' && each.answers.from === 'project') {
          await expect(main(page)).toContainText('Higher is better');
        }
        const name = `new-run/${each.name.replaceAll(' ', '-')}/${width}/${theme}`;
        expect(ledger.unexplained(name, await screenCheck(page, EVERY_CHECK))).toEqual([]);
        if (shots !== undefined) {
          const file = `new-run__${each.name.replaceAll(' ', '-')}__${width}__${theme}.png`;
          await main(page).screenshot({path: `${shots}/${file}`});
          sheet.push({name: `${each.name} ${width} ${theme}`, file});
        }
      });
    }
  }
}

ledger.requireEveryFailure(() => ledger.screens >= (WIDTHS.length + 1) * NEW_RUN_PAGES.length);

test('the contact sheet of the New run pages', async ({browser}) => {
  test.skip(shots === undefined, 'set SCREENS_DIR to write the screenshots and the contact sheet');
  if (shots === undefined) return;
  writeFileSync(`${shots}/new-run-sheet.html`, contactSheetHtml(sheet, 4));
  const page = await browser.newPage({viewport: {width: 3200, height: 1200}});
  await page.goto(`file://${shots}/new-run-sheet.html`);
  await page.screenshot({path: `${shots}/new-run-sheet.png`, fullPage: true});
  await page.close();
});
