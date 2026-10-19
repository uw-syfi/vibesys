import AxeBuilder from '@axe-core/playwright';
import {expect, type Page, test} from '@playwright/test';
import {rawTextOf} from './checks/screen-check.js';
import {card, type MachinesWorld, openMachines, testOf, WORLDS} from './machines-page.js';

function world(name: string): MachinesWorld {
  const found = WORLDS.find(each => each.name === name);
  if (found === undefined) throw new Error(`no world named ${name}`);
  return found;
}

const recorded = (page: Page) =>
  page.evaluate(
    () =>
      (window as unknown as {machinesPreview: {shown: string[]; settings: string[]}})
        .machinesPreview,
  );

test('each machine is a card with its clusters and clouds nested under it', async ({page}) => {
  await openMachines(page, 1440, world('several machines'));
  const names = (machine: string) =>
    card(page, machine)
      .getByRole('row')
      .filter({hasNot: page.getByRole('columnheader')})
      .locator('td:first-child')
      .allTextContents();
  expect(await names('This Mac')).toEqual(['lab-h100', 'campus', 'amd-hpc', 'Modal']);
  expect(await names('host-a')).toEqual(['lab-h100']);
  expect(await names('host-b')).toEqual([]);
  await expect(card(page, 'host-b').getByText('None', {exact: true})).toBeVisible();
  // A helper that predates compute settings lists nothing, and says so in a word.
  await expect(card(page, 'host-c').getByText('Unavailable', {exact: true})).toBeVisible();
  await expect(card(page, 'This Mac').getByText('Online', {exact: true})).toBeVisible();
  await expect(card(page, 'This Mac').getByText('64 CPU · 8× H100')).toBeVisible();
  await expect(card(page, 'This Mac').getByText('2 live', {exact: true})).toBeVisible();
  await expect(card(page, 'This Mac').getByText('3 runs', {exact: true})).toBeVisible();
  await expect(card(page, 'host-a').getByText('1 run', {exact: true})).toBeVisible();
  await expect(page.getByRole('button', {name: 'Add machine'})).toBeVisible();
  expect(await rawTextOf(page)).toEqual([]);
  const axe = await new AxeBuilder({page}).analyze();
  expect(axe.violations).toEqual([]);
});

test('Test says when a cluster was checked, or since when it is unreachable', async ({page}) => {
  await openMachines(page, 1440, world('an unreachable cluster'));
  const row = (cluster: string) =>
    card(page, 'This Mac').getByRole('row', {name: new RegExp(cluster)});
  // The world's test of standby found it unreachable.
  await expect(row('standby')).toContainText(/Unreachable since \d\d:\d\d/);
  await expect(row('lab-h100')).toContainText('Not checked');
  await testOf(page, 'This Mac', 'lab-h100').click();
  await expect(row('lab-h100')).toContainText(/Checked \d\d:\d\d/);
  await expect(row('Modal')).toContainText('Token found');
  await expect(row('Modal').getByRole('button')).toHaveCount(0);
});

test('a test under way says so until it answers', async ({page}) => {
  await openMachines(page, 1000, world('a test in progress'));
  await expect(card(page, 'This Mac').getByRole('row', {name: /lab-h100/})).toContainText(
    'Testing',
  );
  // Another cluster can be tested meanwhile; this one cannot be pressed again.
  await expect(testOf(page, 'This Mac', 'campus')).toHaveText('Test');
  await page.evaluate(() => (window as unknown as {finishTests: () => void}).finishTests());
  await expect(testOf(page, 'This Mac', 'lab-h100')).toHaveText('Test');
  await expect(card(page, 'This Mac').getByRole('row', {name: /lab-h100/})).toContainText(
    /Checked \d\d:\d\d/,
  );
});

test('an unreachable machine lists nothing under it, and Reconnect signs a machine in', async ({
  page,
}) => {
  await openMachines(page, 1440, world('an unreachable machine'));
  await expect(card(page, 'host-a').getByText('Unreachable', {exact: true})).toBeVisible();
  await expect(card(page, 'host-a').getByRole('row')).toHaveCount(0);
  await expect(card(page, 'host-b').getByText('Sign in', {exact: true})).toBeVisible();
  await card(page, 'host-b').getByRole('button', {name: 'Reconnect'}).click();
  await expect(card(page, 'host-b').getByText('Online', {exact: true})).toBeVisible();
  await expect(card(page, 'host-b').getByText('None', {exact: true})).toBeVisible();
});

test('Show runs and Settings name their machine, and Add machine opens its form', async ({
  page,
}) => {
  await openMachines(page, 1440, world('several machines'));
  await card(page, 'host-a').getByRole('button', {name: 'Show runs'}).click();
  await card(page, 'This Mac').getByRole('button', {name: 'Settings'}).click();
  expect(await recorded(page)).toEqual({shown: ['ssh:host-a'], settings: ['local']});
  await page.getByRole('button', {name: 'Add machine'}).click();
  await expect(page.getByRole('textbox', {name: 'SSH host'})).toBeFocused();
});
