import AxeBuilder from '@axe-core/playwright';
import {create, toBinary} from '@bufbuild/protobuf';
import {expect, type Page, test} from '@playwright/test';
import {IntentAction, RunLiveness, WatchRunsResponseSchema} from '@vibesys/proto';
import {Controls} from './chat-controls.js';
import {
  type FakeBridgeSeed,
  installFakeBridge,
  installFakeChrome,
  installFakeMachines,
  installFakeProjectRuns,
  MAC_CHROME,
} from './fake-bridge.js';
import {addInitScripts, initScript} from './init-scripts.js';
import {journeyFixture, journeyHead, journeyScript, transitionJourney} from './journey-world.js';
import {installGatewayOnly} from './retired-run-bridge.js';
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
  chromeInset: MAC_CHROME,
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
  await addInitScripts(page, [
    initScript(installFakeChrome, SEED),
    initScript(installFakeBridge, SEED),
    initScript(installFakeProjectRuns, SEED),
    initScript(installFakeMachines, SEED),
    initScript(installGatewayOnly, undefined),
    journeyScript('prototype'),
  ]);
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
  const last = page.locator('button.vs-dotlegend-item').last();
  await last.click();
  await expect.poll(() => ideas.getByRole('option').count()).toBeLessThan(shown);
  await last.click();
  await expect.poll(() => ideas.getByRole('option').count()).toBe(shown);
  expect((await new AxeBuilder({page}).analyze()).violations).toEqual([]);
});

test('a run that has tried no idea yet says what will appear, not a guess', async ({page}) => {
  await addInitScripts(page, [
    initScript(installFakeChrome, SEED),
    initScript(installFakeBridge, SEED),
    initScript(installFakeProjectRuns, SEED),
    initScript(installFakeMachines, SEED),
    initScript(installGatewayOnly, undefined),
    journeyScript('empty'),
  ]);
  await page.goto('./shell.html');
  await page
    .getByRole('main')
    .getByRole('button', {name: /^queue-rs · mpmc/})
    .click();
  await expect(page.getByRole('listbox', {name: 'Ideas'})).toHaveCount(0);
  await expect(page.getByText('nothing measured yet')).toBeVisible();
});

test('a live run drains from the header over its session, and is let go', async ({page}) => {
  const controls = new Controls(page, journeyFixture('empty'));
  await controls.attach();
  await addInitScripts(page, [
    initScript(installFakeChrome, SEED),
    initScript(installFakeBridge, SEED),
    initScript(installFakeProjectRuns, SEED),
    initScript(installFakeMachines, SEED),
    initScript(installGatewayOnly, undefined),
    journeyScript('empty'),
  ]);
  await page.goto('./shell.html');
  const nav = page.getByRole('navigation', {name: 'VibeSys'});
  await page
    .getByRole('main')
    .getByRole('button', {name: /^queue-rs · mpmc/})
    .click();

  const pause = page.getByRole('button', {name: 'Pause', exact: true});
  await expect(pause).toBeEnabled();
  expect(await commands(page)).toEqual([]);
  await expect
    .poll(() =>
      page.evaluate(() =>
        (window as unknown as {fakeGateway: {active(): number}}).fakeGateway.active(),
      ),
    )
    .toBeGreaterThan(0);
  await pause.click();
  await expect.poll(() => controls.posted.map(request => request.intent.case)).toEqual(['pause']);

  await push(page, listed([server('paused', 'user')]));
  await transitionJourney(page, 'empty', 'parked');
  await controls.settle(IntentAction.PAUSE, journeyHead('parked'));
  // The drained writer has left: the host reports the writer ended, so Resume is admissible.
  await page.evaluate(
    bytes =>
      (window as unknown as {fakeGateway: {observe(b: number[]): void}}).fakeGateway.observe(bytes),
    [
      ...toBinary(
        WatchRunsResponseSchema,
        create(WatchRunsResponseSchema, {
          runId: RUN_ID,
          liveness: RunLiveness.PARKED,
          head: {epoch: 1},
        }),
      ),
    ],
  );
  // A drained pause is parked: the run says Paused, offers Resume, and has nothing left to cut short.
  await expect(page.getByRole('main').getByText('Paused', {exact: true}).first()).toBeVisible();
  await expect(page.getByRole('button', {name: 'Resume', exact: true})).toBeEnabled();
  await expect(page.getByRole('button', {name: 'Pause now', exact: true})).toHaveCount(0);
  expect((await new AxeBuilder({page}).analyze()).violations).toEqual([]);
  if (shots !== undefined) await page.screenshot({path: `${shots}/ink-live-paused.png`});

  await nav.getByRole('button', {name: 'Settings'}).click();
  await expect
    .poll(() =>
      page.evaluate(() =>
        (window as unknown as {fakeGateway: {active(): number}}).fakeGateway.active(),
      ),
    )
    .toBe(0);
  expect(await commands(page)).toEqual([]);
});
