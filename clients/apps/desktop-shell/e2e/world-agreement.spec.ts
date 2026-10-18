import {expect, type Page, test} from '@playwright/test';
import {FIXTURE_NAMES, fixtureNamed} from '@vibesys/run-fixtures';
import {expectNoRequestLoops, openFixture} from './fixture-world.js';

/**
 * One world, one set of counts: for every fixture, the machines and runs that Recent, All runs,
 * Settings' Machines and New run show agree. A machine that has a run, or that New run offers, is
 * never "No machine yet", and a machine with a run in Recent never reads "No runs yet" in All runs.
 */

const nav = (page: Page) => page.getByRole('navigation', {name: 'VibeSys'});

/** The machines the sidebar shows runs under, with how many runs each. */
async function sidebarRuns(page: Page): Promise<Map<string, number>> {
  const groups = await nav(page).locator('.vs-sidebar-group').all();
  const runs = new Map<string, number>();
  for (const group of groups) {
    const title =
      (await group.locator('.vs-sidebar-group-title').first().getAttribute('title')) ?? '';
    const rows = await group.locator('button.vs-sidebar-link').count();
    runs.set(title, (runs.get(title) ?? 0) + rows);
  }
  return runs;
}

async function machinesListed(page: Page): Promise<string[]> {
  await nav(page).getByRole('button', {name: 'Settings'}).click();
  await page
    .getByRole('navigation', {name: 'Settings sections'})
    .getByRole('button', {name: /Machines/})
    .click();
  await expect(page.getByRole('heading', {name: 'Where runs can go'})).toBeVisible();
  await expect(page.getByText('Reading the machines')).toHaveCount(0);
  return page
    .getByRole('tree', {name: 'Machines'})
    .getByRole('treeitem')
    .evaluateAll(rows =>
      rows.map(row => (row.querySelector('.vs-tree-text > *')?.textContent ?? '').trim()),
    );
}

for (const name of FIXTURE_NAMES) {
  test(`the machines and runs agree across every screen: ${name}`, async ({page}) => {
    const fixture = fixtureNamed(name);
    await openFixture(page, fixture);
    const inSidebar = await sidebarRuns(page);
    expect(inSidebar.size).toBeGreaterThan(0);

    const machines = await machinesListed(page);
    const counted = page
      .getByRole('navigation', {name: 'Settings sections'})
      .getByRole('button', {name: /Machines/});
    await expect(counted).toContainText(`${machines.length} saved`);
    await expect(page.getByText('No machine yet')).toHaveCount(0);
    for (const machine of inSidebar.keys()) expect(machines).toContain(machine);

    await nav(page).getByRole('button', {name: 'New run'}).click();
    await page.getByRole('radio', {name: /Your project/}).check();
    await page.getByRole('button', {name: 'Continue'}).click();
    const offered = await page
      .getByRole('combobox', {name: 'Machine'})
      .locator('option')
      .allInnerTexts();
    expect([...offered].sort()).toEqual([...machines].sort());

    await nav(page).getByRole('button', {name: 'Show all'}).click();
    await expect(page.getByRole('heading', {name: 'All runs'})).toBeVisible();
    await expect(page.getByText('Listing runs…')).toHaveCount(0);
    const all = new Map<string, number>();
    for (const group of await page.getByRole('main').locator('.vs-sidebar-group').all()) {
      const title =
        (await group.locator('.vs-sidebar-group-title').first().getAttribute('title')) ?? '';
      all.set(title, await group.locator('button.vs-sidebar-link').count());
    }
    for (const [machine, runs] of inSidebar) {
      expect(machines).toContain(machine);
      expect(all.get(machine) ?? 0, `${machine} in All runs`).toBeGreaterThanOrEqual(runs);
    }
    await expect(page.getByText('No runs yet.')).toHaveCount(0);
    await expectNoRequestLoops(page);
  });
}
