import {mkdirSync} from 'node:fs';
import {expect, type Page, test} from '@playwright/test';
import {fixtureNamed} from '@vibesys/run-fixtures';
import {KnownLedger} from './checks/known-ledger.js';
import {SCREEN_CHECKS, screenCheck} from './checks/screen-check.js';
import {settled} from './checks/settle.js';
import {openFixture, releaseHistory} from './fixture-world.js';

/**
 * A finished run opened while its history is still being read: the run paints from the first reply
 * and the screen says how far the read has come ("History n of N"). It passes the layout and
 * wording checks, keeps its header where it is, then settles once the read ends.
 * `SCREENS_DIR=<dir>` also writes each loading screen's screenshot (the app window only).
 */

/** Where the loading screen and the loaded run draw their shared header: it does not move. */
const HEADER = ['main h1', 'main button[aria-label="More actions"]'];

const fixture = fixtureNamed('finished-run');
const shots = process.env['SCREENS_DIR'] || undefined;
if (shots !== undefined) mkdirSync(shots, {recursive: true});
const ledger = new KnownLedger('run-history');
const WIDTHS = [1000, 1440];
const PREFIXES = [1, 20];

async function headerPlace(page: Page) {
  return Promise.all(
    HEADER.map(async selector => {
      await expect(page.locator(selector).first()).toBeVisible();
      const box = await page.locator(selector).first().boundingBox();
      return box === null ? null : {x: box.x, y: box.y, width: box.width};
    }),
  );
}

for (const width of WIDTHS) {
  for (const prefix of PREFIXES) {
    const name = `finished-run/history-loading/${prefix}-records/${width}`;
    test(name, async ({page}) => {
      await page.setViewportSize({width, height: 900});
      await openFixture(page, fixture, {
        historyPrefix: prefix,
      });
      const main = page.getByRole('main');
      // The run paints from the first reply (the keyframe) and says how far the read has come,
      // in records: there is no total of events or bytes to name any more.
      await expect(main.getByRole('heading', {level: 1})).toBeVisible();
      await expect(main.getByText(/^History \d+ of \d+$/)).toBeVisible();
      await settled(page);
      expect(ledger.unexplained(name, await screenCheck(page, SCREEN_CHECKS))).toEqual([]);
      if (shots !== undefined) {
        await page.screenshot({path: `${shots}/${name.replaceAll('/', '__')}.png`});
      }
      const header = await headerPlace(page);
      await releaseHistory(page);
      await expect(main.getByText(/^History \d+ of \d+$/)).toHaveCount(0);
      await expect(page.getByRole('listbox', {name: 'Ideas'})).toBeVisible();
      await settled(page);
      expect(await headerPlace(page)).toEqual(header);
      await expect(main).not.toContainText('Reading the run’s history…');
    });
  }
}

ledger.requireEveryFailure();
