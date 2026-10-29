import {expect, test} from '@playwright/test';
import {main, nav, openSettingsAt, revealNav} from './settings-machine.js';
import {SEED} from './world.js';

/**
 * Add a machine whose ssh alias does not answer: the form words the failure as a status word under
 * SSH host, as Settings and New run do, and keeps the connection's raw text for the pointer.
 */
const RAW = 'connection down: ssh: connect to host nowhere port 22: Connection refused';

test('Add a machine with an unreachable alias says Unreachable, with the raw text on the pointer', async ({
  page,
}) => {
  await openSettingsAt(page, 1440, {
    ...SEED,
    machines: [{host: 'local', label: 'This Mac', checkout: '~/projs/vibesys'}],
    unreachableHosts: ['ssh:nowhere'],
  });
  await revealNav(page);
  await nav(page)
    .getByRole('button', {name: /Machines/})
    .click();
  await page.getByRole('button', {name: 'Add a machine'}).click();
  await page.getByRole('textbox', {name: 'SSH host'}).fill('nowhere');
  await page.getByRole('combobox', {name: 'VibeSys checkout'}).fill('~/vibesys');
  await page.getByRole('button', {name: 'Check and add'}).click();
  const word = main(page).getByText('Unreachable', {exact: true});
  await expect(word).toBeVisible();
  await expect(word).toHaveAttribute('title', RAW);
  await expect(main(page).getByText(/Connection refused/)).toHaveCount(0);
  // The form stays open to be corrected, and nothing was added.
  await expect(page.getByRole('button', {name: 'Check and add'})).toBeVisible();
});
