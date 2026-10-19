import {expect, type Page, test} from '@playwright/test';
import type {FakeBridgeSeed} from './fake-bridge.js';
import {type FakeHostWindow, listed, open, SEED, server, sidebar} from './world.js';

/**
 * The app's home follows whether it knows of any run: with none on any machine it is New run and
 * the rail has no Runs; once a machine lists one, Runs appears and is the home. The mark goes home
 * in both cases.
 */

test.use({viewport: {width: 1440, height: 900}});

const NO_RUNS: FakeBridgeSeed = {
  ...SEED,
  listings: [
    {kind: 'listed', host: 'local', live: [], ended: [], unreadable: [], unverified: []},
    listed([], []),
  ],
  recent: [],
  projectRuns: {},
};

const rail = (page: Page, name: string) => sidebar(page).getByRole('button', {name, exact: true});

/** New run's first page names no run list: it is the form, with the rail marking New. */
async function expectNewRun(page: Page): Promise<void> {
  await expect(rail(page, 'New')).toHaveAttribute('aria-current', 'page');
  await expect(page.getByRole('heading', {name: 'Runs', level: 1})).toHaveCount(0);
}

test('with no run on any machine the app opens on New run and the rail has no Runs', async ({
  page,
}) => {
  await open(page, NO_RUNS);
  await expectNewRun(page);
  await expect(rail(page, 'Runs')).toHaveCount(0);
  await expect(rail(page, 'Machines')).toBeVisible();
});

test('the mark goes to New run while there is no run, from any screen', async ({page}) => {
  await open(page, NO_RUNS);
  await rail(page, 'Usage').click();
  await expect(rail(page, 'Usage')).toHaveAttribute('aria-current', 'page');
  await rail(page, 'VibeSys').click();
  await expectNewRun(page);
});

test('a run appearing adds Runs to the rail, and the mark then goes to the dashboard', async ({
  page,
}) => {
  await open(page, NO_RUNS);
  await expect(rail(page, 'Runs')).toHaveCount(0);
  await page.evaluate(
    listing => (window as unknown as FakeHostWindow).fakeHost.listing(listing),
    listed([server('00000000000a', '20261015-163047', 'running')], []),
  );
  await expect(rail(page, 'Runs')).toBeVisible();
  // The user is not taken from New run by it.
  await expectNewRun(page);
  await rail(page, 'Usage').click();
  await rail(page, 'VibeSys').click();
  await expect(page.getByRole('heading', {name: 'Runs', level: 1})).toBeVisible();
  await expect(rail(page, 'Runs')).toHaveAttribute('aria-current', 'page');
});

test('with a run listed the app opens on the Runs dashboard', async ({page}) => {
  await open(page);
  await expect(rail(page, 'Runs')).toBeVisible();
  await expect(page.getByRole('heading', {name: 'Runs', level: 1})).toBeVisible();
});

test('a configured machine with a live run lists it on a fresh profile with no recent run', async ({
  page,
}) => {
  await open(page, {...SEED, recent: [], projectRuns: {}});
  await expect(rail(page, 'Runs')).toBeVisible();
  await expect(page.getByRole('heading', {name: 'Runs', level: 1})).toBeVisible();
});
