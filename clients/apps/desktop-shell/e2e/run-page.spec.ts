import AxeBuilder from '@axe-core/playwright';
import {create, fromBinary, toBinary} from '@bufbuild/protobuf';
import {expect, type Page, test} from '@playwright/test';
import {HostCallRequestSchema, HostCallResponseSchema} from '@vibesys/proto';
import {intentReplay} from '@vibesys/run-fixtures';
import {
  type FakeBridgeSeed,
  installFakeBridge,
  installFakeChrome,
  installFakeMachines,
  installFakeNewRun,
  installFakeProjectRuns,
  MAC_CHROME,
} from './fake-bridge.js';
import {addInitScripts, initScript} from './init-scripts.js';
import {journeyScript, type NativeJourney, transitionJourney} from './journey-world.js';
import {installGatewayOnly} from './retired-run-bridge.js';
import {STARTED_AT} from './started-at.js';

/** The Run screen starts, drains, cancels, and resumes through its shipping bridge. */

interface FakeHostWindow extends Window {
  fakeHost: {commands: string[]; listing(listing: unknown): void; calls: unknown[][]};
}

const PROJECT = '/home/me/vibesys-projects/queue-rs';
const RUN_ID = '20261015-163047';
const INSTANCE = '00000000000a';

function server(phase: string, end: unknown = null): Record<string, unknown> {
  return {
    id: INSTANCE,
    projectRoot: PROJECT,
    runId: RUN_ID,
    status: phase,
    phase,
    paused: null,
    end,
    startedAt: STARTED_AT,
    reason: null,
  };
}

function listed(live: unknown[], ended: unknown[]): Record<string, unknown> {
  return {kind: 'listed', host: 'ssh:host-b', live, ended, unreadable: [], unverified: []};
}

const SEED: FakeBridgeSeed = {
  theme: 'ink',
  chromeInset: MAC_CHROME,
  appearance: {mode: 'dark', light: 'paper', dark: 'ink'},
  listings: [listed([server('starting')], [])],
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
  projectRuns: {[`ssh:host-b|${PROJECT}`]: [{runId: RUN_ID, task: 'mpmc', createdAt: 3}]},
};

/** The live run's server: a session that opens and answers, with no events to show yet. */
async function open(page: Page, seed: FakeBridgeSeed): Promise<void> {
  await addInitScripts(page, [
    initScript(installFakeChrome, seed),
    initScript(installFakeBridge, seed),
    initScript(installFakeMachines, seed),
    initScript(installFakeNewRun, seed),
    initScript(installFakeProjectRuns, seed),
    initScript(installGatewayOnly, undefined),
    journeyScript(
      (seed.listings?.[0] as {ended?: unknown[]})?.ended?.length
        ? 'stopped-empty'
        : (seed.listings?.[0] as {live?: {phase: string}[]})?.live?.[0]?.phase === 'starting'
          ? 'startup'
          : 'empty',
    ),
  ]);
  await page.goto('./shell.html');
  if ((seed.listings?.[0] as {live?: {phase: string}[]})?.live?.[0]?.phase === 'starting')
    phases.set(page, 'startup');
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'ink');
  await page
    .getByRole('main')
    .getByRole('button', {name: /^queue-rs · mpmc/})
    .click();
}

const phases = new WeakMap<Page, NativeJourney>();
const shots = process.env['RUN_PAGE_SHOTS'];

async function shot(page: Page, name: string): Promise<void> {
  if (shots !== undefined) await page.screenshot({path: `${shots}/${name}.png`, fullPage: true});
}

async function push(page: Page, listing: unknown): Promise<void> {
  await page.evaluate(
    ([value]) => (window as unknown as FakeHostWindow).fakeHost.listing(value),
    [listing],
  );
  const from = phases.get(page);
  if (from) {
    const value = listing as {live: {phase: string}[]; ended: unknown[]};
    const phase = value.live[0]?.phase;
    const to = value.ended.length
      ? 'startup-ended'
      : phase === 'running'
        ? 'startup-running'
        : phase === 'stopping'
          ? 'startup-draining'
          : undefined;
    if (!to) throw new Error(`Unsupported startup fixture phase: ${phase}`);
    await transitionJourney(page, from, to);
    phases.set(page, to);
  }
}

test.use({colorScheme: 'dark'});

test('a run starts, writes its logs, permits cancellation during drain, and resumes after ending', async ({
  page,
}) => {
  await page.clock.install();
  // The host admits what the page posts: a pending intent, or the epoch a relaunch starts.
  const posted: string[] = [];
  await page.exposeFunction('postIntent', (_host: string, bytes: number[]) => {
    const {method} = fromBinary(HostCallRequestSchema, new Uint8Array(bytes));
    if (method.case === 'relaunch') {
      posted.push('relaunch');
      return [
        ...toBinary(
          HostCallResponseSchema,
          create(HostCallResponseSchema, {
            result: {case: 'relaunch', value: {outcome: {case: 'epoch', value: 2n}}},
          }),
        ),
      ];
    }
    if (method.case !== 'postIntent') throw new Error('Expected PostIntent');
    posted.push(method.value.intent.case ?? '');
    return [
      ...toBinary(
        HostCallResponseSchema,
        intentReplay('pending', method.value.requestId).admission,
      ),
    ];
  });
  await open(page, SEED);

  // Starting: the header and the startup timeline, the active step showing its live log.
  await expect(page.getByRole('heading', {name: 'queue-rs · mpmc'})).toBeVisible();
  await expect(page.getByText('host-b · 20261015-163047')).toBeVisible();
  await expect(page.getByText('Starting', {exact: true}).first()).toBeVisible();
  const startup = page.getByRole('region', {name: 'Starting'});
  await expect(startup.getByText('Starting the agent container')).toBeVisible();
  await expect(startup.getByText('[readiness] claude: binary ok')).toBeVisible();
  expect((await new AxeBuilder({page}).analyze()).violations).toEqual([]);
  await shot(page, 'ink-starting');

  // Running: the timeline goes; the Logs face groups the streams by owner and follows one.
  await push(page, listed([server('running')], []));
  await expect(startup).toHaveCount(0);
  await expect(page.getByRole('button', {name: 'Pause', exact: true})).toBeEnabled();
  await page.getByRole('radiogroup', {name: 'Show'}).getByText('Logs').click();
  const streams = page.getByRole('listbox', {name: 'Log streams'});
  for (const group of ['Startup', 'Gates', 'Tool builds', 'Run narration']) {
    await expect(streams.getByRole('option', {name: group}).first()).toBeVisible();
  }
  // A measurement's candidate build is its own gate stream, before the gates after it.
  await streams.getByRole('option', {name: /candidate build · input · attempt 1/}).click();
  await expect(page.getByRole('log').getByText('Finished release profile')).toBeVisible();
  await streams
    .getByRole('option', {name: /Run narration/})
    .last()
    .click();
  await expect(page.getByRole('log').getByText('16:36:20 INFO container ready')).toBeVisible();
  expect((await new AxeBuilder({page}).analyze()).violations).toEqual([]);
  await shot(page, 'ink-logs');

  // Pause now is immediately available while a graceful Pause drains, and is admitted at once.
  await push(page, listed([server('stopping')], []));
  await expect(page.getByText('Pausing', {exact: true}).first()).toBeVisible();
  await expect(
    page.getByText('Unfinished work may need repeating and may still incur charges.'),
  ).toBeVisible();
  await shot(page, 'ink-pause-now');
  await page.getByRole('button', {name: 'Pause now', exact: true}).click();
  await expect.poll(() => posted).toEqual(['pauseNow']);

  // Ended: the banner reads the end record; Resume names the round, only when resumable.
  await push(
    page,
    listed(
      [],
      [
        server('failed', {
          outcome: 'failed',
          reason: 'W6’s benchmark timed out after 300 s.',
          resumable: true,
          resume_from_round: 2,
        }),
      ],
    ),
  );
  await expect(page.getByText('W6’s benchmark timed out after 300 s.').first()).toBeVisible();
  expect((await new AxeBuilder({page}).analyze()).violations).toEqual([]);
  await shot(page, 'ink-ended');
  await page.getByRole('button', {name: 'Resume', exact: true}).click();
  await expect.poll(() => posted.at(-1)).toBe('relaunch');
});

test('the run level says what will appear before anything is measured, and keeps the search', async ({
  page,
}) => {
  await open(page, {...SEED, listings: [listed([server('running')], [])]});

  // With nothing folded yet the hero and the ideas say what will appear, not a guess.
  await expect(page.getByText('nothing measured yet')).toBeVisible();
  await expect(page.getByRole('listbox', {name: 'Ideas'})).toHaveCount(0);
  expect((await new AxeBuilder({page}).analyze()).violations).toEqual([]);
  await shot(page, 'ink-run-empty');

  // The mode lives in the zoom, the filters in the page state: a switch away and back keeps both.
  const show = page.getByRole('radiogroup', {name: 'Show'});
  await show.getByText('Agents').click();
  await expect(show.getByRole('radio', {name: 'Agents'})).toBeChecked();
  await show.getByText('Ideas').click();
  await expect(show.getByRole('radio', {name: 'Ideas'})).toBeChecked();
  await expect(page.getByRole('listbox', {name: 'Ideas'})).toHaveCount(0);
});

test('every ended run offers Resume', async ({page}) => {
  const stopped = server('stopped', {
    outcome: 'stopped',
    reason: 'Stopped by you.',
    resumable: false,
  });
  await open(page, {...SEED, listings: [listed([], [stopped])]});
  await page.locator('.vs-banner').getByRole('button', {name: 'Show details'}).click();
  await expect(page.getByText('Stopped by you.')).toBeVisible();
  await expect(page.getByRole('button', {name: 'Resume', exact: true})).toBeVisible();
  await page.getByRole('button', {name: 'View logs'}).click();
  await expect(
    page.getByRole('radiogroup', {name: 'Show'}).getByRole('radio', {name: 'Logs'}),
  ).toBeChecked();
});
