import {expect, test} from '@playwright/test';
import {card, openMachines, rail, WORLDS} from './machines-page.js';
import {hosts} from './subscription-fake.js';

/**
 * The Machines page over a scripted host's live stats: what each state shows on its card, that a
 * change of state reaches the screen, that Retry is a request main hears, and that the page watches
 * only while it is on screen.
 */

const world = (name: string) => {
  const found = WORLDS.find(each => each.name === name);
  if (found === undefined) throw new Error(`no world ${name}`);
  return found;
};

const gauges = (page: import('@playwright/test').Page, machine: string) =>
  card(page, machine).locator('.vs-gauge');

test('a live host shows its figures, and a change of state reaches its card', async ({page}) => {
  await openMachines(page, 1440, world('a live machine'));
  await expect(gauges(page, 'This Mac')).toHaveText(['CPU34%', 'Memory18.0 / 36.0 GB']);
  await expect(card(page, 'This Mac').locator('.vs-gauge[data-dim]')).toHaveCount(0);
  await hosts.set(page, 'local', {
    kind: 'stale',
    reason: 'silent',
    ageMs: 125_000,
    readings: {cpuPercent: 80},
  });
  // Dimmed and never hidden, with how old the value is.
  await expect(card(page, 'This Mac').locator('.vs-gauge[data-dim]')).toHaveCount(2);
  await expect(gauges(page, 'This Mac').first()).toHaveText('CPU80%');
  await expect(card(page, 'This Mac').getByText('Stale 2 min')).toBeVisible();
  await hosts.set(page, 'local', {kind: 'live', readings: {cpuPercent: 12}});
  await expect(gauges(page, 'This Mac').first()).toHaveText('CPU12%');
  await expect(card(page, 'This Mac').locator('.vs-gauge[data-dim]')).toHaveCount(0);
  await expect(card(page, 'This Mac').getByText(/Stale/)).toHaveCount(0);
});

test('a host with GPUs shows their use and memory beside CPU and memory', async ({page}) => {
  await openMachines(page, 1440, world('two live machines'));
  await expect(gauges(page, 'host-a')).toHaveText([
    'CPU71%',
    'Memory212 / 512 GB',
    'GPU68%',
    'VRAM416 / 640 GB',
  ]);
  await expect(gauges(page, 'This Mac')).toHaveCount(2);
});

test('a failed host says why on its own card, and Retry asks main to try again', async ({page}) => {
  await openMachines(page, 1440, world('a failed machine'));
  const mac = card(page, 'This Mac');
  await expect(mac.getByText('Unreachable', {exact: true})).toBeVisible();
  await expect(mac.locator('.vs-gauge')).toHaveCount(0);
  // The header status is the machine's own; the live stats never replace it with a banner.
  await expect(page.locator('[role="alert"]')).toHaveCount(0);
  await mac.getByRole('button', {name: 'Retry'}).click();
  await expect.poll(() => hosts.retried(page)).toEqual(['local']);
  await expect(gauges(page, 'This Mac')).toHaveText(['CPU34%', 'Memory18.0 / 36.0 GB']);
  await expect(mac.getByRole('button', {name: 'Retry'})).toHaveCount(0);
});

test('every state of a host is a word or a figure on its card, and the others are unaffected', async ({
  page,
}) => {
  await openMachines(page, 1440, world('machines in every live state'));
  await expect(gauges(page, 'This Mac')).toHaveCount(2);
  await expect(card(page, 'host-a').getByText(/^Stale/)).toBeVisible();
  await expect(card(page, 'host-b').getByText('Unreachable', {exact: true})).toBeVisible();
  await expect(card(page, 'host-c').getByText('Connecting', {exact: true})).toBeVisible();
});

test('the page watches a host only while it is on screen', async ({page}) => {
  await openMachines(page, 1440, world('a live machine'));
  await expect.poll(() => hosts.observing(page, 'local')).toBe(1);
  await rail(page).getByRole('button', {name: 'Runs'}).click();
  await expect.poll(() => hosts.observing(page, 'local')).toBe(0);
  await rail(page).getByRole('button', {name: 'Machines'}).click();
  await expect.poll(() => hosts.observing(page, 'local')).toBe(1);
  await expect(gauges(page, 'This Mac')).toHaveCount(2);
});
