import AxeBuilder from '@axe-core/playwright';
import {expect, type Page, test} from '@playwright/test';
import {
  type FakeBridgeSeed,
  installFakeBridge,
  installFakeChrome,
  installFakeMachines,
  installFakeNewRun,
  MAC_CHROME,
} from '../src/testing/fake-bridge.js';
import {rawTextOf} from './checks/screen-check.js';
import {addInitScripts, initScript} from './init-scripts.js';
import {showScreen} from './read.js';

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
  chromeInset: MAC_CHROME,
  appearance: {mode: 'dark', light: 'paper', dark: 'ink'},
  listings: [],
  recent: [],
  machines: [
    {host: 'local', label: 'This Mac', checkout: '~/projs/vibesys'},
    {host: 'ssh:host-a', label: 'host-a', checkout: '/data/vibesys'},
    {host: 'ssh:host-b', label: 'host-b', checkout: '~/vibesys'},
  ],
  reports: {
    'ssh:host-a': {kind: 'sign-in', detail: 'your SSH key needs its passphrase.'},
    'ssh:host-b': {kind: 'checkout-not-found', detail: '~/vibesys on host-b is not a directory.'},
  },
  facts: {
    checkout: {root: '/home/me/vibesys', version: '0.1.1', commit: 'e66d941', dirty: false},
    hardware: {cpu_cores: 64, gpus: [], accelerator: 'cpu'},
    docker: {reachable: true, version: '29.1'},
    harness_clis: ['claude', 'codex'],
    harness_cli_names: {claude: 'Claude Code', codex: 'Codex'},
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

async function openMachines(page: Page, seed: FakeBridgeSeed = SEED): Promise<void> {
  await addInitScripts(page, [
    initScript(installFakeChrome, seed),
    initScript(installFakeBridge, seed),
    initScript(installFakeMachines, seed),
    initScript(installFakeNewRun, seed),
  ]);
  await page.goto('./shell.html');
  await showScreen(page, 'Settings');
  await page
    .getByRole('navigation', {name: 'Settings sections'})
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
  await expect(row(page, 'host-a')).toContainText('Sign in');
  await expect(row(page, 'host-b')).toContainText('Checkout not found');
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
  await row(page, 'host-b').click();
  // The status word carries the reason as its description; the checkout's path is shown by its last name only.
  await expect(
    page
      .getByRole('main')
      .locator('[aria-description^="Nothing is at vibesys on host-b any more."]'),
  ).toBeAttached();
  const fix = page.getByRole('combobox', {name: 'VibeSys checkout on host-b'});
  await expect(fix).toHaveValue('~/vibesys');
  await shoot(page, 'ink-checkout-not-found');
  await fix.fill('/nowhere');
  await page.getByRole('button', {name: 'Check and save'}).click();
  await expect(page.getByText('/nowhere on host-b is not a directory.')).toBeVisible();
  await fix.fill('/home/me/src/vibesys');
  await page.getByRole('button', {name: 'Check and save'}).click();
  await expect(row(page, 'host-b')).toContainText('Ready');
  await expect(page.getByRole('definition').first()).toHaveText('/home/me/src/vibesys');

  // Sign in is the connection's own action.
  await row(page, 'host-a').click();
  await expect(
    page
      .getByRole('main')
      .locator('[aria-description="The app couldn’t connect: your SSH key needs its passphrase."]'),
  ).toBeAttached();
  await shoot(page, 'ink-sign-in');
  await page.getByRole('button', {name: 'Sign in', exact: true}).click();
  await expect(row(page, 'host-a')).toContainText('Ready');

  const axe = await new AxeBuilder({page}).analyze();
  expect(axe.violations).toEqual([]);
});

test('add a machine, change a checkout from labelled suggestions, and remove a machine', async ({
  page,
}) => {
  await openMachines(page);
  await page.getByRole('button', {name: 'Add a machine'}).click();
  await page.getByRole('textbox', {name: 'SSH host'}).fill('host-c');
  await page.getByRole('combobox', {name: 'VibeSys checkout'}).fill('~/vibesys');
  await shoot(page, 'ink-add');
  await page.getByRole('button', {name: 'Check and add'}).click();
  await expect(row(page, 'host-c')).toContainText('Ready');
  await expect(page.getByRole('heading', {name: 'host-c'})).toBeVisible();
  // The count in the navigation is the list beside it, as machines come and go.
  const nav = page
    .getByRole('navigation', {name: 'Settings sections'})
    .getByRole('button', {name: /Machines/});
  await expect(machineList(page).getByRole('treeitem')).toHaveCount(4);
  await expect(nav).toContainText('4 saved · 2 need attention');

  // Change checkout offers the checkouts it finds, each with its version.
  await row(page, 'This Mac').click();
  await page.getByRole('button', {name: 'Change checkout'}).click();
  const field = page.getByRole('combobox', {name: 'VibeSys checkout on This Mac'});
  await field.fill('/home/me/');
  const options = page.getByRole('listbox').getByRole('option');
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
  await row(page, 'host-c').click();
  await page.getByRole('button', {name: 'Remove'}).click();
  await expect(row(page, 'host-c')).toHaveCount(0);
  await expect(machineList(page).getByRole('treeitem')).toHaveCount(3);
  await expect(nav).toContainText('3 saved · 2 need attention');
  await row(page, 'This Mac').click();
  await expect(page.getByRole('button', {name: 'Remove'})).toHaveCount(0);

  // New run here opens New run.
  await page.getByRole('button', {name: 'New run here'}).click();
  await expect(page.getByRole('heading', {name: 'What do you want to optimize?'})).toBeVisible();
});

test('a machine whose VibeSys lacks what the app needs shows Needs an update, not Ready', async ({
  page,
}) => {
  await openMachines(page, {
    ...SEED,
    reports: {...SEED.reports, 'ssh:host-a': {kind: 'needs-update', facts: null}},
  });
  await expect(row(page, 'host-a')).toContainText('Needs an update');
  await expect(row(page, 'host-a')).not.toContainText('Ready');
  await row(page, 'host-a').click();
  await expect(
    page.getByRole('main').locator('[aria-description="VibeSys on host-a needs an update"]'),
  ).toBeAttached();
  expect(await rawTextOf(page)).toEqual([]);
});

test('the machines screen names its harnesses and hardware, never their ids', async ({page}) => {
  await openMachines(page);
  await expect(page.getByText('Claude Code · Codex installed')).toBeVisible();
  expect(await rawTextOf(page)).toEqual([]);
});

test('Escape closes Add a machine, which has the focus as it opens, and no empty state sits beside it', async ({
  page,
}) => {
  const none: FakeBridgeSeed = {...SEED, machines: []};
  await addInitScripts(page, [
    initScript(installFakeChrome, none),
    initScript(installFakeBridge, none),
    initScript(installFakeMachines, none),
    initScript(installFakeNewRun, none),
  ]);
  await page.goto('./shell.html');
  await showScreen(page, 'Settings');
  await page
    .getByRole('navigation', {name: 'Settings sections'})
    .getByRole('button', {name: /Machines/})
    .click();
  await expect(page.getByText('No machine yet')).toBeVisible();

  await page.getByRole('button', {name: 'Add a machine'}).click();
  await expect(page.getByRole('textbox', {name: 'SSH host'})).toBeFocused();
  await expect(page.getByText('No machine yet')).toHaveCount(0);

  await page.keyboard.press('Escape');
  await expect(page.getByRole('textbox', {name: 'SSH host'})).toHaveCount(0);
  await expect(page.getByText('No machine yet')).toBeVisible();
});

test('Escape in the checkout field first hides its suggestions, and closes the form the next time', async ({
  page,
}) => {
  await openMachines(page);
  await page.getByRole('button', {name: 'Add a machine'}).click();
  await page.getByRole('textbox', {name: 'SSH host'}).fill('host-a');
  const checkout = page.getByRole('combobox', {name: 'VibeSys checkout'});
  await checkout.fill('/home/me/');
  await expect(page.getByRole('listbox').getByRole('option').first()).toBeVisible();

  await page.keyboard.press('Escape');
  await expect(page.getByRole('listbox').getByRole('option')).toHaveCount(0);
  await expect(checkout).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(checkout).toHaveCount(0);
});
