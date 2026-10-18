import {mkdirSync} from 'node:fs';
import {expect, type Page, test} from '@playwright/test';
import {fixtureNamed} from '@vibesys/run-fixtures';
import {KNOWN_FAILURES} from './checks/known-failures.js';
import {report, screenCheck} from './checks/screen-check.js';
import {settled} from './checks/settle.js';
import {openFixture, releaseHistory} from './fixture-world.js';

/**
 * A finished run opened while its history is still being read: the screen says so, with how far
 * the read has come (a bar and "n of N events" when the host says the total, else the events and
 * bytes so far), and shows nothing derived from the prefix: no "running" count, no Now pane, no
 * outcome. It passes the layout and wording checks, then settles into the run once the read ends.
 * `SCREENS_DIR=<dir>` also writes each loading screen's screenshot (the app window only).
 */

/** Where the loading screen and the loaded run draw their shared header: it does not move. */
const HEADER = ['main h1', 'main button[aria-label="More actions"]'];

const fixture = fixtureNamed('finished-run');
const shots = process.env['SCREENS_DIR'] || undefined;
if (shots !== undefined) mkdirSync(shots, {recursive: true});

async function headerPlace(page: Page) {
  return Promise.all(
    HEADER.map(async selector => {
      const box = await page.locator(selector).first().boundingBox();
      return box === null ? null : {x: box.x, y: box.y, width: box.width};
    }),
  );
}

for (const width of [1000, 1440]) {
  for (const total of [true, false]) {
    const name = `finished-run/history-loading/${total ? 'determinate' : 'indeterminate'}/${width}`;
    test(name, async ({page}) => {
      await page.setViewportSize({width, height: 900});
      await openFixture(page, fixture, {
        historyServing: {pageSize: 10, hold: 2, total},
      });
      const main = page.getByRole('main');
      await expect(main).toContainText(
        total ? `20 of ${fixture.events.length} events` : '20 events',
      );
      if (total) {
        await expect(main.getByRole('img', {name: 'Share of the run’s events read'})).toBeVisible();
      } else {
        await expect(main.getByRole('img')).toHaveCount(0);
        await expect(main).toContainText('read so far');
      }
      await expect(main).not.toContainText(/running|\bnow\b/i);
      await expect(page.getByRole('listbox', {name: 'Ideas'})).toHaveCount(0);
      await expect(page.getByRole('slider', {name: 'Run position'})).toHaveCount(0);
      await settled(page);
      const result = report(name, await screenCheck(page), KNOWN_FAILURES);
      expect(
        result.unexplained.map(each => `[${each.check}] ${each.message} (${each.where})`),
      ).toEqual([]);
      if (shots !== undefined) {
        await page.screenshot({path: `${shots}/${name.replaceAll('/', '__')}.png`});
      }
      const header = await headerPlace(page);
      await releaseHistory(page);
      await expect(page.getByRole('listbox', {name: 'Ideas'})).toBeVisible();
      await settled(page);
      expect(await headerPlace(page)).toEqual(header);
      await expect(main).not.toContainText('Reading the run’s history…');
    });
  }
}
