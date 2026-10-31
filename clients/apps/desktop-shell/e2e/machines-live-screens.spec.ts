import {mkdirSync} from 'node:fs';
import {expect, test} from '@playwright/test';
import {screenCheck} from './checks/screen-check.js';
import {openMachines, resizeTo, WORLDS} from './machines-page.js';

/**
 * The live states of the Machines page, in both themes at the two widths the page is judged at: the
 * layout checks hold on each, and `SCREENS_DIR=<dir>` writes each screenshot (the page only).
 */

const LIVE = [
  'a live machine',
  'a stale machine',
  'a failed machine',
  'two live machines',
  'machines in every live state',
] as const;
const THEMES = [
  {name: 'light', theme: 'paper', mode: 'light'},
  {name: 'dark', theme: 'ink', mode: 'dark'},
] as const;
const WIDTHS = [1440, 1000] as const;

const shots = process.env['SCREENS_DIR'] || undefined;
if (shots !== undefined) mkdirSync(shots, {recursive: true});

for (const name of LIVE) {
  for (const {name: look, theme, mode} of THEMES) {
    test(`${name} in ${look} passes the layout checks at each width`, async ({page}) => {
      const world = WORLDS.find(each => each.name === name);
      if (world === undefined) throw new Error(`no world ${name}`);
      const seed = {...world.seed, theme, appearance: {mode, light: 'paper', dark: 'ink'}};
      await openMachines(page, WIDTHS[0], {...world, seed});
      for (const width of WIDTHS) {
        await resizeTo(page, width);
        expect(await screenCheck(page), `at ${width}px`).toEqual([]);
        if (shots !== undefined) {
          const file = `machines-live__${name.replaceAll(' ', '-')}__${look}__${width}.png`;
          await page.getByRole('main').screenshot({path: `${shots}/${file}`});
        }
      }
    });
  }
}
