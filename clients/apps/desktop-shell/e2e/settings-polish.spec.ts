import {expect, type Page, test} from '@playwright/test';
import {
  installFakeBridge,
  installFakeChrome,
  installFakeMachines,
  installFakeNewRun,
} from '../src/testing/fake-bridge.js';
import {addInitScripts, initScript} from './init-scripts.js';
import {showScreen} from './read.js';
import {revealNav} from './settings-machine.js';
import {SEED} from './world.js';

/**
 * Settings and All runs at the widths users see. Every width is set with `setViewportSize` and
 * measured (`innerWidth`) before it is reported, so a "660 px" check really is 660 px wide.
 */

const WIDTHS = [1440, 1000, 660] as const;
const HEIGHT = 800;
const settingsNav = (page: Page) => page.getByRole('navigation', {name: 'Settings sections'});

async function resize(page: Page, width: number): Promise<void> {
  await page.setViewportSize({width, height: HEIGHT});
  expect(await page.evaluate(() => window.innerWidth)).toBe(width);
}

async function openSettings(
  page: Page,
  width: number,
  machines: readonly {host: string; label: string; checkout: string}[] = SEED.machines ?? [],
): Promise<void> {
  const world = {...SEED, machines};
  await resize(page, width);
  await addInitScripts(page, [
    initScript(installFakeChrome, world),
    initScript(installFakeBridge, world),
    initScript(installFakeMachines, world),
    initScript(installFakeNewRun, world),
  ]);
  await page.goto('./shell.html');
  expect(await page.evaluate(() => window.innerWidth)).toBe(width);
  await showScreen(page, 'Settings');
}

const openMachines = async (page: Page): Promise<void> => {
  await revealNav(page);
  await settingsNav(page)
    .getByRole('button', {name: /Machines/})
    .click();
  await expect(page.getByRole('heading', {name: 'Where runs can go'})).toBeVisible();
};

for (const width of WIDTHS) {
  test(`with no saved machine the empty state fills the content at ${width} px`, async ({page}) => {
    await openSettings(page, width, []);
    await openMachines(page);
    const card = page.locator('main .vs-card', {hasText: 'No machine yet'});
    await expect(card).toBeVisible();
    const box = await card.boundingBox();
    const heading = await page.getByRole('heading', {name: 'Where runs can go'}).boundingBox();
    const add = await page.getByRole('button', {name: 'Add a machine'}).boundingBox();
    if (box === null || heading === null || add === null) throw new Error('nothing drawn');
    expect(box.x).toBeCloseTo(heading.x, 0);
    expect(box.x + box.width).toBeCloseTo(add.x + add.width, 0);
    await expect(page.getByRole('tree', {name: 'Machines'})).toHaveCount(0);
  });

  test(`each theme group's swatches share one row at ${width} px`, async ({page}) => {
    await openSettings(page, width);
    const groups = await page.evaluate(() =>
      [...document.querySelectorAll('main .vs-grid')].map(grid =>
        [...grid.querySelectorAll('.vs-swatch')].map(swatch => {
          const box = swatch.getBoundingClientRect();
          return {top: Math.round(box.top), width: Math.round(box.width)};
        }),
      ),
    );
    expect(groups).toHaveLength(2);
    for (const swatches of groups) {
      expect(swatches).toHaveLength(3);
      expect(new Set(swatches.map(swatch => swatch.top)).size).toBe(1);
      expect(new Set(swatches.map(swatch => swatch.width)).size).toBe(1);
    }
  });
}
