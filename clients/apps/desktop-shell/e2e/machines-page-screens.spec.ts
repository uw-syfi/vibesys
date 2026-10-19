import {mkdirSync} from 'node:fs';
import {expect, test} from '@playwright/test';
import {screenCheck} from './checks/screen-check.js';
import {openMachines, WORLDS} from './machines-page.js';

/**
 * The Machines page at the three window widths the app is used at, over each world: one machine,
 * several, an unreachable machine, an unreachable cluster, a machine with nothing set up, and a
 * test under way. The layout checks hold on each; the width is the page's own `innerWidth`, read
 * back. `SCREENS_DIR=<dir>` writes each screenshot (the page only).
 */

const shots = process.env['SCREENS_DIR'] || undefined;
if (shots !== undefined) mkdirSync(shots, {recursive: true});

for (const width of [1440, 1000, 660] as const) {
  for (const world of WORLDS) {
    test(`${world.name} at ${width}px passes the layout checks`, async ({page}) => {
      await openMachines(page, width, world);
      expect(await screenCheck(page)).toEqual([]);
      if (shots !== undefined) {
        const file = `machines__${world.name.replaceAll(' ', '-')}__${width}.png`;
        await page.getByRole('main').screenshot({path: `${shots}/${file}`});
      }
    });
  }
}
