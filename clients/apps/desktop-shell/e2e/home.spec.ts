import {expect, type Page, test} from '@playwright/test';
import type {FakeBridgeSeed} from './fake-bridge.js';
import {type FakeHostWindow, listed, open, SEED, server, sidebar} from './world.js';

/**
 * The app never changes screen by itself. It opens on Runs, which shows each machine's steps while
 * the machines answer and says there is no run when they hold none, with a New run action. Runs is
 * always in the rail. The mark goes home, which is New run while no run is known and the Runs
 * dashboard once a machine lists one.
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

test('with no run on any machine the app opens on Runs, which says so and offers New run', async ({
  page,
}) => {
  await open(page, NO_RUNS);
  await expect(page.getByRole('heading', {name: 'Runs', level: 1})).toBeVisible();
  await expect(page.getByText('No runs', {exact: true})).toBeVisible();
  await expect(rail(page, 'Machines')).toBeVisible();
  await page.getByRole('button', {name: 'New run', exact: true}).click();
  await expectNewRun(page);
  await expect(rail(page, 'Runs')).toBeVisible();
});

test('the mark goes to New run while there is no run, from any screen', async ({page}) => {
  await open(page, NO_RUNS);
  await rail(page, 'Usage').click();
  await expect(rail(page, 'Usage')).toHaveAttribute('aria-current', 'page');
  await rail(page, 'VibeSys').click();
  await expectNewRun(page);
});

test('a run appearing sends the mark to the dashboard, without leaving New run', async ({page}) => {
  await open(page, NO_RUNS);
  await rail(page, 'VibeSys').click();
  await expectNewRun(page);
  await page.evaluate(
    listing => (window as unknown as FakeHostWindow).fakeHost.listing(listing),
    listed([server('00000000000a', '20261015-163047', 'running')], []),
  );
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

test('a machine that has not answered shows its steps, and a stopped one can be retried', async ({
  page,
}) => {
  const machines = [
    {host: 'local', label: 'This Mac', checkout: '/bundled'},
    {host: 'ssh:host-a', label: 'host-a', checkout: '/checkout'},
  ];
  await open(page, {
    ...NO_RUNS,
    machines,
    listings: [
      {kind: 'unavailable', host: 'local', step: 'handshake', connection: {kind: 'connecting'}},
      {
        kind: 'unavailable',
        host: 'ssh:host-a',
        step: 'gateway',
        connection: {kind: 'offline', detail: 'gateway startup exited: 1'},
      },
    ],
  });
  const local = page.locator('.vs-step-list', {hasText: 'This Mac'});
  await expect(local.getByRole('listitem')).toHaveText([
    /Starting gateway/,
    /Connecting.*2\/3/,
    /Loading runs/,
  ]);
  await expect(local.getByRole('button', {name: 'Retry'})).toHaveCount(0);
  await expect(rail(page, 'Runs')).toHaveAttribute('aria-current', 'page');

  const remote = page.locator('.vs-step-list', {hasText: 'host-a'});
  await expect(remote.getByRole('img', {name: 'Failed'})).toBeVisible();
  await expect(remote).toContainText('gateway exited (code 1)');
  await remote.getByRole('button', {name: 'Retry'}).click();
  await expect
    .poll(() => page.evaluate(() => (window as unknown as FakeHostWindow).fakeHost.retried))
    .toEqual(['ssh:host-a']);

  await page.evaluate(
    each => {
      const host = (window as unknown as FakeHostWindow).fakeHost;
      for (const listing of each) host.listing(listing);
    },
    machines.map(({host}) => ({
      kind: 'listed',
      host,
      live: [],
      ended: [],
      unreadable: [],
      unverified: [],
    })),
  );
  await expect(page.getByText('No runs', {exact: true})).toBeVisible();
  await expect(page.locator('.vs-step-list')).toHaveCount(0);
});
