import {expect, type Page, test} from '@playwright/test';
import {fixtureNamed} from '@vibesys/run-fixtures';
import {
  installFakeBridge,
  installFakeChrome,
  installFakeMachines,
  installFakeNewRun,
} from '../src/testing/fake-bridge.js';
import {openFixture} from './fixture-world.js';
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

const ADD_CASES = [
  {name: 'nothing typed', host: '', checkout: '', field: 'SSH host', says: /host is required/i},
  {name: 'no host', host: '', checkout: '~/vibesys', field: 'SSH host', says: /host is required/i},
  {name: 'a bad host', host: '-oX=1', checkout: '~/vibesys', field: 'SSH host', says: /usable ssh/},
  {name: 'a known host', host: 'host-b', checkout: '~/vibesys', field: 'SSH host', says: /already/},
  {name: 'no checkout', host: 'host-d', checkout: '', field: 'VibeSys checkout', says: /required/},
  {
    name: 'a bad path',
    host: 'host-d',
    checkout: 'vibesys',
    field: 'VibeSys checkout',
    says: /absolute/,
  },
] as const;

for (const each of ADD_CASES) {
  test(`Add a machine with ${each.name} puts its error under ${each.field}`, async ({page}) => {
    await openSettings(page, 1000);
    await openMachines(page);
    await page.getByRole('button', {name: 'Add a machine'}).click();
    await page.getByRole('textbox', {name: 'SSH host'}).fill(each.host);
    await page.getByRole('combobox', {name: 'VibeSys checkout'}).fill(each.checkout);
    await page.getByRole('button', {name: 'Check and add'}).click();

    // Each control's description is the error text below it, and only the named field is invalid.
    const fields = await page.evaluate(() =>
      [...document.querySelectorAll('input[aria-label], input[id]')].map(input => {
        const label = input.id ? document.querySelector(`label[for="${input.id}"]`) : null;
        const errors = (input.getAttribute('aria-describedby') ?? '')
          .split(' ')
          .map(id => document.getElementById(id))
          .filter(node => node?.getAttribute('data-tone') === 'err')
          .map(node => node?.textContent ?? '');
        return {
          label: label?.textContent ?? '',
          invalid: input.getAttribute('aria-invalid') === 'true',
          errors,
        };
      }),
    );
    const named = fields.filter(field => field.invalid);
    expect(named.map(field => field.label)).toEqual([each.field]);
    expect(named[0]?.errors).toHaveLength(1);
    expect(named[0]?.errors[0]).toMatch(each.says);
    expect(named[0]?.errors[0]).not.toContain('""');
  });
}

test('the Add form shows placeholders as examples, never as values', async ({page}) => {
  await openSettings(page, 1000);
  await openMachines(page);
  await page.getByRole('button', {name: 'Add a machine'}).click();
  const placeholders = await page.evaluate(() =>
    [...document.querySelectorAll('main input[placeholder]')].map(input =>
      input.getAttribute('placeholder'),
    ),
  );
  expect(placeholders).toHaveLength(2);
  for (const placeholder of placeholders) expect(placeholder).toMatch(/^e\.g\. /);
});

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

for (const width of WIDTHS) {
  test(`the Runs dashboard lists the live run of a fixture, once, at ${width} px`, async ({
    page,
  }) => {
    await resize(page, width);
    await openFixture(page, fixtureNamed('weeks-600-ideas'));
    expect(await page.evaluate(() => window.innerWidth)).toBe(width);
    await showScreen(page, 'Runs');
    await expect(page.getByRole('heading', {name: 'Runs'})).toBeVisible();
    await expect(page.locator('main .vs-overview-tile')).toHaveCount(1);
  });
}
