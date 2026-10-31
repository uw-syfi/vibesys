import {mkdirSync} from 'node:fs';
import {expect, test} from '@playwright/test';
import {KnownLedger} from './checks/known-ledger.js';
import {EVERY_CHECK, screenCheck} from './checks/screen-check.js';
import {openMachines, resizeTo, WORLDS} from './machines-page.js';

/**
 * The Machines page, reached by the rail, over each world, opened once and resized across the three window widths the
 * app is used at: one machine,
 * several, an unreachable machine, an unreachable cluster, a machine with nothing set up, and a
 * test under way. The layout checks hold on each; the width is the page's own `innerWidth`, read
 * back. `SCREENS_DIR=<dir>` writes each screenshot (the page only).
 */

const WIDTHS = [1440, 1000, 660] as const;

const shots = process.env['SCREENS_DIR'] || undefined;
if (shots !== undefined) mkdirSync(shots, {recursive: true});
const ledger = new KnownLedger('machines');

for (const world of WORLDS) {
  test(`${world.name} passes the layout checks at each width`, async ({page}) => {
    await openMachines(page, WIDTHS[0], world);
    for (const width of WIDTHS) {
      await resizeTo(page, width);
      const name = `machines/${world.name.replaceAll(' ', '-')}/${width}`;
      expect(ledger.unexplained(name, await screenCheck(page, EVERY_CHECK))).toEqual([]);
      if (shots !== undefined) {
        const file = `machines__${world.name.replaceAll(' ', '-')}__${width}.png`;
        await page.getByRole('main').screenshot({path: `${shots}/${file}`});
      }
    }
  });
}

ledger.requireEveryFailure(() => ledger.screens >= WIDTHS.length * WORLDS.length);
