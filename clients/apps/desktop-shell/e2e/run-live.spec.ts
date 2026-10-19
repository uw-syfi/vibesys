import AxeBuilder from '@axe-core/playwright';
import {expect, type Page, test} from '@playwright/test';
import {
  type FakeBridgeSeed,
  installFakeBridge,
  installFakeMachines,
  installFakeProjectRuns,
} from './fake-bridge.js';
import {installFakeRun} from './fake-run.js';
import {prototypeRun} from './fake-run-data.js';
import {type FakeServerSeed, installFakeServer} from './fake-server.js';
import {STARTED_AT} from './started-at.js';

/**
 * The Run screen's live connection against the Fake bridge and a fake run server: selecting a live
 * run attaches it and opens its session over the relayed port, Pause and Resume go to the run as
 * protocol commands once the session can deliver them, and leaving the run lets it go. Checked in
 * the Ink theme; `RUN_PAGE_SHOTS` names a directory to save the screens in.
 */

interface FakeHostWindow extends Window {
  fakeHost: {listing(listing: unknown): void; commands: string[]};
}

const PROJECT = '/home/me/vibesys-projects/queue-rs';
const RUN_ID = '20261015-163047';
const INSTANCE = '00000000000a';

function server(phase: string, paused: string | null = null): Record<string, unknown> {
  return {
    id: INSTANCE,
    projectRoot: PROJECT,
    runId: RUN_ID,
    status: phase,
    phase,
    paused,
    end: null,
    startedAt: STARTED_AT,
    reason: null,
  };
}

function listed(live: unknown[]): Record<string, unknown> {
  return {kind: 'listed', host: 'ssh:host-b', live, ended: [], unreadable: [], unverified: []};
}

const SEED: FakeBridgeSeed = {
  theme: 'ink',
  chromeInset: {width: 84, height: 40},
  appearance: {mode: 'dark', light: 'paper', dark: 'ink'},
  listings: [listed([server('running')])],
  recent: [
    {
      host: 'ssh:host-b',
      hostLabel: 'host-b',
      project: PROJECT,
      task: 'mpmc',
      runId: RUN_ID,
      instanceId: INSTANCE,
      attachedAt: 3,
    },
  ],
};

const SERVER: FakeServerSeed = {
  runId: RUN_ID,
  batch: {
    type: 'event_batch',
    events: [{timestamp: '2026-10-15T16:30:51Z', type: 'server_ready', sequence: 1}],
    through_sequence: 1,
    store_id: 'store-1',
    history_after_sequence: 0,
  },
  snapshot: {run_id: RUN_ID, sequence: 1, status: 'running'},
};

const shots = process.env['RUN_PAGE_SHOTS'];

async function commands(page: Page): Promise<string[]> {
  return page.evaluate(() => [...(window as unknown as FakeHostWindow).fakeHost.commands]);
}

async function push(page: Page, listing: unknown): Promise<void> {
  await page.evaluate(
    ([value]) => (window as unknown as FakeHostWindow).fakeHost.listing(value),
    [listing],
  );
}

test.use({colorScheme: 'dark'});

test('a live run folds its trace into the ideas, the failed listed and hideable', async ({
  page,
}) => {
  await page.addInitScript(installFakeBridge, SEED);
  await page.addInitScript(installFakeProjectRuns, SEED);
  await page.addInitScript(installFakeMachines, SEED);
  await page.addInitScript(installFakeRun, {streams: [], text: {}});
  await page.addInitScript(installFakeServer, prototypeRun(RUN_ID));
  await page.goto('./shell.html');
  await page
    .getByRole('main')
    .getByRole('button', {name: /^queue-rs · mpmc/})
    .click();

  const ideas = page.getByRole('listbox', {name: 'Ideas'});
  await expect(ideas.getByRole('option').first()).toBeVisible();
  expect((await new AxeBuilder({page}).analyze()).violations).toEqual([]);
  if (shots !== undefined)
    await page.getByRole('main').screenshot({path: `${shots}/ink-live-ideas.png`});
  const shown = await ideas.getByRole('option').count();
  await expect(page.getByRole('button', {name: /^\+ \d+ failed$/})).toHaveCount(0);
  // A status chip narrows the list to its group and shows every idea again when pressed twice.
  await page.locator('button.vs-dotlegend-item', {hasText: /\d+ failed/}).click();
  await expect.poll(() => ideas.getByRole('option').count()).toBeLessThan(shown);
  await page.locator('button.vs-dotlegend-item', {hasText: /\d+ failed/}).click();
  await expect.poll(() => ideas.getByRole('option').count()).toBe(shown);
  expect((await new AxeBuilder({page}).analyze()).violations).toEqual([]);
});

test('a run that has tried no idea yet says what will appear, not a guess', async ({page}) => {
  await page.addInitScript(installFakeBridge, SEED);
  await page.addInitScript(installFakeProjectRuns, SEED);
  await page.addInitScript(installFakeMachines, SEED);
  await page.addInitScript(installFakeRun, {streams: [], text: {}});
  await page.addInitScript(installFakeServer, SERVER);
  await page.goto('./shell.html');
  await page
    .getByRole('main')
    .getByRole('button', {name: /^queue-rs · mpmc/})
    .click();
  await expect(page.getByText('No ideas yet')).toBeVisible();
  await expect(page.getByText('nothing measured yet')).toBeVisible();
});

test('a live run pauses and resumes from the header over its session, and is let go', async ({
  page,
}) => {
  await page.addInitScript(installFakeBridge, SEED);
  await page.addInitScript(installFakeProjectRuns, SEED);
  await page.addInitScript(installFakeMachines, SEED);
  await page.addInitScript(installFakeRun, {streams: [], text: {}});
  await page.addInitScript(installFakeServer, SERVER);
  await page.goto('./shell.html');
  const nav = page.getByRole('navigation', {name: 'VibeSys'});
  await page
    .getByRole('main')
    .getByRole('button', {name: /^queue-rs · mpmc/})
    .click();

  const pause = page.getByRole('button', {name: 'Pause'});
  await expect(pause).toBeEnabled();
  await expect.poll(() => commands(page)).toEqual([`attach ${INSTANCE}`]);
  await pause.click();
  await expect.poll(() => commands(page)).toContain('command.pause');

  await push(page, listed([server('paused', 'user')]));
  await expect(page.getByRole('main').getByText('Paused by you')).toBeVisible();
  const resume = page.getByRole('button', {name: 'Resume'});
  await expect(resume).toBeEnabled();
  expect((await new AxeBuilder({page}).analyze()).violations).toEqual([]);
  if (shots !== undefined) await page.screenshot({path: `${shots}/ink-live-paused.png`});
  await resume.click();
  await expect.poll(() => commands(page)).toContain('command.resume');

  await nav.getByRole('button', {name: 'Settings'}).click();
  await expect.poll(() => commands(page)).toContain('release');
});
