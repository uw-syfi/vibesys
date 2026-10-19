import {expect, type Page, test} from '@playwright/test';
import {FIXTURE_NAMES, fixtureNamed} from '@vibesys/run-fixtures';
import {expectNoRequestLoops, openFixture} from './fixture-world.js';

/**
 * One world, one set of counts: for every fixture, the machines and runs that the Runs dashboard,
 * Settings' Machines and New run show agree. A machine that has a run, or that New run offers, is
 * never "No machine yet", and a machine with a run is never missing from Settings' Machines.
 */

const nav = (page: Page) => page.getByRole('navigation', {name: 'VibeSys'});

/** The machines the dashboard shows runs under, with how many runs each. */
async function dashboardRuns(page: Page): Promise<Map<string, number>> {
  await nav(page).getByRole('button', {name: /^Runs/}).click();
  await expect(page.getByRole('heading', {name: 'Runs'})).toBeVisible();
  await page.getByRole('combobox', {name: 'Group by'}).selectOption('machine');
  const groups = await page.locator('.vs-overview-group').all();
  const runs = new Map<string, number>();
  for (const group of groups) {
    const title = (await group.getAttribute('aria-label')) ?? '';
    if (title === 'Needs you') continue;
    runs.set(title, (runs.get(title) ?? 0) + (await group.locator('.vs-overview-tile').count()));
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
    const onDashboard = await dashboardRuns(page);
    expect(onDashboard.size).toBeGreaterThan(0);

    const machines = await machinesListed(page);
    const counted = page
      .getByRole('navigation', {name: 'Settings sections'})
      .getByRole('button', {name: /Machines/});
    await expect(counted).toContainText(`${machines.length} saved`);
    await expect(page.getByText('No machine yet')).toHaveCount(0);
    for (const machine of onDashboard.keys()) expect(machines).toContain(machine);

    await nav(page).getByRole('button', {name: 'New', exact: true}).click();
    await page.getByRole('radio', {name: /Your project/}).check();
    await page.getByRole('button', {name: 'Continue'}).click();
    const offered = await page
      .getByRole('combobox', {name: 'Machine'})
      .locator('option')
      .allInnerTexts();
    expect([...offered].sort()).toEqual([...machines].sort());
    await expectNoRequestLoops(page);
  });
}
