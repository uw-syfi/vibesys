import {mkdirSync, writeFileSync} from 'node:fs';
import {expect, test} from '@playwright/test';
import {contactSheetHtml, type SheetEntry} from './checks/contact-sheet.js';
import {KnownLedger} from './checks/known-ledger.js';
import {EVERY_CHECK, screenCheck} from './checks/screen-check.js';
import {main, openSettingsAt} from './settings-machine.js';
import {SETTINGS_PAGES} from './settings-machine-pages.js';

/**
 * Every page of machine-scoped Settings at the three window widths the app is used at: the layout
 * checks (controls in a row align, columns start at one top, nothing overflows or overlaps, and the
 * rest) hold on each. The width is the page's own `innerWidth`, read back, never assumed.
 * `SCREENS_DIR=<dir>` writes each page's screenshot (the page only) and a contact sheet.
 */

const shots = process.env['SCREENS_DIR'] || undefined;
if (shots !== undefined) mkdirSync(shots, {recursive: true});
const sheet: SheetEntry[] = [];
const ledger = new KnownLedger('settings');

const WIDTHS = [1440, 1000, 660] as const;

for (const width of WIDTHS) {
  for (const each of SETTINGS_PAGES) {
    test(`${each.name} at ${width}px passes the layout checks`, async ({page}) => {
      await openSettingsAt(page, width);
      await each.reach(page);
      expect(await page.evaluate(() => window.innerWidth), 'the window is as wide as asked').toBe(
        width,
      );
      const name = `settings/${each.name.replaceAll(' ', '-')}/${width}`;
      expect(ledger.unexplained(name, await screenCheck(page, EVERY_CHECK))).toEqual([]);
      if (shots !== undefined) {
        const file = `settings-machine__${each.name.replaceAll(' ', '-')}__${width}.png`;
        await main(page).screenshot({path: `${shots}/${file}`});
        sheet.push({name: `${each.name} ${width}`, file});
      }
    });
  }
}

ledger.requireEveryFailure(() => ledger.screens >= WIDTHS.length * SETTINGS_PAGES.length);

test('the contact sheet of the machine Settings pages', async ({browser}) => {
  test.skip(shots === undefined, 'set SCREENS_DIR to write the screenshots and the contact sheet');
  if (shots === undefined) return;
  writeFileSync(`${shots}/settings-machine-sheet.html`, contactSheetHtml(sheet, 4));
  const page = await browser.newPage({viewport: {width: 3200, height: 1200}});
  await page.goto(`file://${shots}/settings-machine-sheet.html`);
  await page.screenshot({path: `${shots}/settings-machine-sheet.png`, fullPage: true});
  await page.close();
});
