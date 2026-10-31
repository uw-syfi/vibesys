/**
 * The pages of machine-scoped Settings, each reached the way a user reaches it, so the layout
 * checks and the every-control-acts check look at the same screens.
 */
import {expect, type Page} from '@playwright/test';
import {main, openSection, revealNav} from './settings-machine.js';

export interface SettingsPage {
  readonly name: string;
  /** Brings the page up, from the Settings screen just opened. */
  reach(page: Page): Promise<void>;
}

async function openForm(page: Page): Promise<void> {
  await main(page).getByRole('button', {name: 'Add a cluster'}).click();
  await expect(main(page).getByRole('heading', {name: 'Add a Slurm cluster'})).toBeVisible();
}

async function fillForm(page: Page, alias: string): Promise<void> {
  await main(page).getByRole('textbox', {name: 'Name'}).fill('lab-h100');
  await main(page)
    .getByRole('textbox', {name: 'Workspace on the cluster'})
    .fill('/scratch/vibesys');
  await main(page).getByRole('textbox', {name: 'SSH host alias'}).fill(alias);
  await main(page).getByRole('button', {name: 'Test connection'}).click();
}

export const SETTINGS_PAGES: readonly SettingsPage[] = [
  {
    name: 'clusters',
    reach: async page => {
      await openSection(page, 'Clusters and clouds');
      await expect(main(page).getByRole('heading', {name: 'cluster-m'})).toBeVisible();
    },
  },
  {
    name: 'clusters with the sections open',
    reach: async page => {
      await openSection(page, 'Clusters and clouds');
      await revealNav(page);
      await expect(main(page).getByRole('heading', {name: 'cluster-m'})).toBeVisible();
    },
  },
  {
    name: 'clusters empty',
    reach: async page => {
      await openSection(page, 'Clusters and clouds', 'host-b');
      await expect(main(page).getByText('No Slurm cluster yet')).toBeVisible();
    },
  },
  {
    name: 'clusters add form',
    reach: async page => {
      await openSection(page, 'Clusters and clouds', 'host-b');
      await openForm(page);
    },
  },
  {
    name: 'clusters add form tested',
    reach: async page => {
      await openSection(page, 'Clusters and clouds', 'host-b');
      await openForm(page);
      await fillForm(page, 'lab-login');
      await expect(main(page).getByText('The connection works.')).toBeVisible();
    },
  },
  {
    name: 'clusters add form failed',
    reach: async page => {
      await openSection(page, 'Clusters and clouds', 'host-b');
      await openForm(page);
      await fillForm(page, 'no-workspace');
      // The failed test is a toast over the page.
      await expect(page.getByRole('alert').getByText('The workspace is not usable')).toBeVisible();
    },
  },
  {
    name: 'clusters login node',
    reach: async page => {
      await openSection(page, 'Clusters and clouds', 'host-b');
      await openForm(page);
      await main(page).getByRole('radio', {name: 'This machine is a login node'}).check();
    },
  },
  {
    name: 'clusters too old',
    reach: async page => {
      await openSection(page, 'Clusters and clouds', 'host-c');
      await expect(main(page).getByText('Needs an update', {exact: true})).toBeVisible();
    },
  },
  {
    name: 'logins',
    reach: async page => {
      await openSection(page, 'Agent logins');
      await expect(main(page).getByRole('heading', {name: 'Aider'})).toBeVisible();
    },
  },
  {
    name: 'logins started',
    reach: async page => {
      await openSection(page, 'Agent logins', 'host-b');
      await main(page).getByRole('button', {name: 'Sign in'}).first().click();
      await expect(main(page).getByText('The login started on host-b.')).toBeVisible();
    },
  },
];
