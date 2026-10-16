import AxeBuilder from '@axe-core/playwright';
import {expect, type Page, test} from '@playwright/test';
import {type FakeBridgeSeed, installFakeBridge, installFakeMachines} from './fake-bridge.js';

/** Where the journey's Ink screenshots go, when `MACHINES_SCREENSHOTS` names a directory. */
const SHOTS = process.env['MACHINES_SCREENSHOTS'];

const FOUND = {
  state: 'found',
  root: '/home/me/vibesys',
  version: '0.1.1',
  commit: 'e66d941',
  dirty: false,
  compatible: true,
};

const SEED: FakeBridgeSeed = {
  theme: 'ink',
  chromeInset: {width: 84, height: 40},
  appearance: {mode: 'dark', light: 'paper', dark: 'ink'},
  listings: [],
  recent: [],
  machines: [
    {host: 'local', label: 'This Mac', checkout: '~/projs/vibesys'},
    {host: 'ssh:chelan3', label: 'chelan3', checkout: '/mnt/data/shli/vibesys'},
    {host: 'ssh:chelan4', label: 'chelan4', checkout: '~/vibesys'},
  ],
  reports: {
    'ssh:chelan3': {kind: 'sign-in', detail: 'your SSH key needs its passphrase.'},
    'ssh:chelan4': {kind: 'checkout-not-found', detail: '~/vibesys on chelan4 is not a directory.'},
  },
  facts: {
    checkout: {root: '/home/me/vibesys', version: '0.1.1', commit: 'e66d941', dirty: false},
    hardware: {cpu_cores: 64, gpus: [], accelerator: 'cpu'},
    docker: {reachable: true, version: '29.1'},
    harness_clis: ['claude', 'codex'],
    runs: {live: 1, ended_today: 2},
  },
  checkouts: {
    '~/vibesys': FOUND,
    '/home/me/src/vibesys': FOUND,
    '/home/me/vibesys': FOUND,
    '/home/me/vibesys-old': {state: 'too_old', version: '0.0.9', min: '0.1.0'},
  },
  directories: {
    '/home/me/': [
      {name: 'notes', path: '/home/me/notes', checkout: false, project: false},
      {name: 'vibesys', path: '/home/me/vibesys', checkout: true, project: false},
      {name: 'vibesys-old', path: '/home/me/vibesys-old', checkout: true, project: false},
    ],
  },
};

const machineList = (page: Page) => page.getByRole('tree', {name: 'Machines'});
const row = (page: Page, name: string) => machineList(page).getByRole('treeitem', {name});

async function openMachines(page: Page): Promise<void> {
  await page.addInitScript(installFakeBridge, SEED);
  await page.addInitScript(installFakeMachines, SEED);
  await page.goto('./shell.html');
  await page
    .getByRole('navigation', {name: 'VibeSys'})
    .getByRole('button', {name: /Machines/})
    .click();
  await expect(page.getByRole('heading', {name: 'Where runs can go'})).toBeVisible();
}

async function shoot(page: Page, name: string): Promise<void> {
  if (SHOTS !== undefined) await page.screenshot({path: `${SHOTS}/${name}.png`, fullPage: true});
}

test('machines show their status from their facts, and each problem carries its fix', async ({
  page,
}) => {
  await openMachines(page);
  await expect(row(page, 'This Mac')).toContainText('Ready');
  await expect(row(page, 'chelan3')).toContainText('Sign in');
  await expect(row(page, 'chelan4')).toContainText('Checkout not found');
  await expect(page.getByRole('term')).toHaveText([
    'Checkout',
    'Version',
    'Hardware',
    'Docker',
    'Harnesses',
    'Runs',
  ]);
  await expect(page.getByText('64 CPU cores · no GPU')).toBeVisible();
  await shoot(page, 'ink-ready');

  // A missing checkout is fixed inline: its field is open with the old path.
  await row(page, 'chelan4').click();
  await expect(page.getByText('Nothing is at ~/vibesys on chelan4 any more.')).toBeVisible();
  const fix = page.getByRole('combobox', {name: 'VibeSys checkout on chelan4'});
  await expect(fix).toHaveValue('~/vibesys');
  await shoot(page, 'ink-checkout-not-found');
  await fix.fill('/nowhere');
  await page.getByRole('button', {name: 'Check and save'}).click();
  await expect(page.getByText('/nowhere on chelan4 is not a directory.')).toBeVisible();
  await fix.fill('/home/me/src/vibesys');
  await page.getByRole('button', {name: 'Check and save'}).click();
  await expect(row(page, 'chelan4')).toContainText('Ready');
  await expect(page.getByRole('definition').first()).toHaveText('/home/me/src/vibesys');

  // Sign in is the connection's own action.
  await row(page, 'chelan3').click();
  await expect(
    page.getByText('The app couldn’t connect: your SSH key needs its passphrase.'),
  ).toBeVisible();
  await shoot(page, 'ink-sign-in');
  await page.getByRole('button', {name: 'Sign in'}).click();
  await expect(row(page, 'chelan3')).toContainText('Ready');

  const axe = await new AxeBuilder({page}).analyze();
  expect(axe.violations).toEqual([]);
});

test('add a machine, change a checkout from labelled suggestions, and remove a machine', async ({
  page,
}) => {
  await openMachines(page);
  await page.getByRole('button', {name: 'Add a machine'}).click();
  await page.getByRole('textbox', {name: 'SSH host'}).fill('chelan5');
  await page.getByRole('combobox', {name: 'VibeSys checkout'}).fill('~/vibesys');
  await shoot(page, 'ink-add');
  await page.getByRole('button', {name: 'Check and add'}).click();
  await expect(row(page, 'chelan5')).toContainText('Ready');
  await expect(page.getByRole('heading', {name: 'chelan5'})).toBeVisible();

  // Change checkout offers the checkouts it finds, each with its version.
  await row(page, 'This Mac').click();
  await page.getByRole('button', {name: 'Change checkout'}).click();
  const field = page.getByRole('combobox', {name: 'VibeSys checkout on This Mac'});
  await field.fill('/home/me/');
  const options = page.getByRole('option');
  await expect(options).toHaveText([
    '/home/me/notes/',
    '/home/me/vibesys/0.1.1 · e66d941',
    '/home/me/vibesys-old/0.0.9 · too old for this app',
  ]);
  await shoot(page, 'ink-change-checkout');
  await options.nth(1).click();
  await expect(field).toHaveValue('/home/me/vibesys/');
  await page.getByRole('button', {name: 'Check and save'}).click();
  await expect(page.getByRole('definition').first()).toHaveText('/home/me/vibesys');

  // Remove forgets the machine; This Mac cannot be removed.
  await row(page, 'chelan5').click();
  await page.getByRole('button', {name: 'Remove'}).click();
  await expect(row(page, 'chelan5')).toHaveCount(0);
  await row(page, 'This Mac').click();
  await expect(page.getByRole('button', {name: 'Remove'})).toHaveCount(0);

  // New run here opens New run.
  await page.getByRole('button', {name: 'New run here'}).click();
  await expect(page.getByRole('heading', {name: 'New run'})).toBeVisible();
});
