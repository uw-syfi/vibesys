import AxeBuilder from '@axe-core/playwright';
import {expect, type Page, test} from '@playwright/test';
import {
  type FakeBridgeSeed,
  installFakeBridge,
  installFakeMachines,
  installFakeNewRun,
  installFakeProjectRuns,
} from './fake-bridge.js';
import {type FakeRunSeed, installFakeRun} from './fake-run.js';
import {type FakeServerSeed, installFakeServer} from './fake-server.js';
import {STARTED_AT} from './started-at.js';

/**
 * The Run screen's journey against the Fake bridge and a fake run that reports its startup steps
 * and writes its logs: open it from the sidebar, watch it start, read its logs, stop it (with Force
 * stop offered only once stopping has taken two minutes on the page's clock), and see it end.
 * Screens are checked in the Ink theme; `RUN_PAGE_SHOTS` names a directory to save them in.
 */

interface FakeHostWindow extends Window {
  fakeHost: {listing(listing: unknown): void; calls: unknown[][]};
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

function stream(id: string, owner: Record<string, unknown>, outcome: string | null) {
  return {id, owner, command: 'docker build .', retained_start: 0, size: 0, outcome};
}

const NARRATION = [
  {at: '2026-10-15T16:30:51+00:00', message: 'run started (2 rounds)'},
  {at: '2026-10-15T16:36:20+00:00', message: 'container ready'},
]
  .map(({at, message}) =>
    JSON.stringify({at, level: 'INFO', logger: 'vibesys', thread: 'main', message}),
  )
  .join('\n');

const SEED: FakeBridgeSeed = {
  theme: 'ink',
  chromeInset: {width: 84, height: 40},
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
const SERVER: FakeServerSeed = {
  runId: RUN_ID,
  batch: {
    type: 'event_batch',
    events: [{timestamp: '2026-10-15T16:30:51Z', type: 'server_ready', sequence: 1}],
    through_sequence: 1,
    store_id: 'store-1',
    history_after_sequence: 0,
  },
  snapshot: {run_id: RUN_ID, sequence: 1, status: 'starting'},
};

const RUN: FakeRunSeed = {
  streams: [
    stream('s1', {kind: 'startup_step', stage: 'building_image'}, 'ended'),
    stream('s2', {kind: 'startup_step', stage: 'starting_container'}, null),
    stream('s6', {kind: 'gate_stage', execution_id: 'w5', gate: 'build', attempt: 1}, 'ended'),
    stream('s3', {kind: 'gate_stage', execution_id: 'w5', gate: 'benchmark', attempt: 1}, null),
    stream('s4', {kind: 'tool_build', name: 'perf'}, 'ended'),
    stream('s5', {kind: 'run_narration'}, null),
  ],
  text: {
    s1: '#8 [7/8] WORKDIR /workspace\n#8 DONE 0.0s\n#9 exporting to image\n',
    s2: '[docker] starting container\n[readiness] claude: binary ok\n',
    s3: 'vibesys-queue benchmark, 3 repetitions\nrepetition 1: 41.2 Mops/s\n',
    s4: 'perf: cached\n',
    s6: 'cargo build --release --locked\nFinished release profile\n',
    s5: `${NARRATION}\n`,
  },
};

async function open(page: Page, seed: FakeBridgeSeed): Promise<void> {
  await page.addInitScript(installFakeBridge, seed);
  await page.addInitScript(installFakeMachines, seed);
  await page.addInitScript(installFakeNewRun, seed);
  await page.addInitScript(installFakeProjectRuns, seed);
  await page.addInitScript(installFakeRun, RUN);
  await page.addInitScript(installFakeServer, SERVER);
  await page.goto('./shell.html');
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'ink');
  await page.getByRole('main').getByRole('button', {name: /^mpmc/}).click();
}

const shots = process.env['RUN_PAGE_SHOTS'];

async function shot(page: Page, name: string): Promise<void> {
  if (shots !== undefined) await page.screenshot({path: `${shots}/${name}.png`, fullPage: true});
}

async function push(page: Page, listing: unknown): Promise<void> {
  await page.evaluate(
    ([value]) => (window as unknown as FakeHostWindow).fakeHost.listing(value),
    [listing],
  );
}

async function calls(page: Page): Promise<unknown[][]> {
  return page.evaluate(() => (window as unknown as FakeHostWindow).fakeHost.calls);
}

test.use({colorScheme: 'dark'});

test('a run starts, writes its logs, stops with Force stop after the bound, and ends', async ({
  page,
}) => {
  await page.clock.install();
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
  await expect(page.getByRole('button', {name: 'Pause'})).toBeEnabled();
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

  // Stop: the confirmation says what is lost; Force stop only after two minutes of stopping.
  await page.getByRole('button', {name: 'Stop', exact: true}).click();
  await expect(page.getByText('cannot be resumed')).toBeVisible();
  await shot(page, 'ink-stop-confirm');
  await page.getByRole('button', {name: 'Stop run'}).click();
  await expect.poll(() => calls(page)).toEqual([['stopConfirmed', 'ssh:host-b', INSTANCE, false]]);
  await push(page, listed([server('stopping')], []));
  const force = page.getByRole('button', {name: 'Force stop'});
  await page.clock.fastForward('01:59');
  await expect(page.getByText('Stopping · waiting')).toBeVisible();
  await expect(force).toHaveCount(0);
  await page.clock.fastForward('00:02');
  await expect(force).toBeVisible();
  await shot(page, 'ink-force-stop');
  await force.click();
  await expect
    .poll(() => calls(page))
    .toEqual([
      ['stopConfirmed', 'ssh:host-b', INSTANCE, false],
      ['stopConfirmed', 'ssh:host-b', INSTANCE, true],
    ]);

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
  await expect(page.getByText('W6’s benchmark timed out after 300 s.')).toBeVisible();
  await expect(force).toHaveCount(0);
  expect((await new AxeBuilder({page}).analyze()).violations).toEqual([]);
  await shot(page, 'ink-ended');
  await page.getByRole('button', {name: 'Resume from round 2'}).click();
  await expect
    .poll(async () => (await calls(page)).at(-1))
    .toEqual(['resume', 'ssh:host-b', PROJECT, RUN_ID]);
});

test('the run level says what will appear before anything is measured, and keeps the search', async ({
  page,
}) => {
  await open(page, {...SEED, listings: [listed([server('running')], [])]});

  // With nothing folded yet the hero and the ideas say what will appear, not a guess.
  await expect(page.getByText('nothing measured yet')).toBeVisible();
  await expect(page.getByText('No ideas yet')).toBeVisible();
  expect((await new AxeBuilder({page}).analyze()).violations).toEqual([]);
  await shot(page, 'ink-run-empty');

  // The mode lives in the zoom, the filters in the page state: a switch away and back keeps both.
  const show = page.getByRole('radiogroup', {name: 'Show'});
  await show.getByText('Agents').click();
  await expect(show.getByRole('radio', {name: 'Agents'})).toBeChecked();
  await show.getByText('Ideas').click();
  await expect(show.getByRole('radio', {name: 'Ideas'})).toBeChecked();
  await expect(page.getByText('No ideas yet')).toBeVisible();
});

test('a cleanly stopped run offers no Resume', async ({page}) => {
  const stopped = server('stopped', {
    outcome: 'stopped',
    reason: 'Stopped by you.',
    resumable: false,
  });
  await open(page, {...SEED, listings: [listed([], [stopped])]});
  await expect(page.getByText('Stopped by you.')).toBeVisible();
  await expect(page.getByRole('button', {name: /^Resume/})).toHaveCount(0);
  await page.getByRole('button', {name: 'View logs'}).click();
  await expect(
    page.getByRole('radiogroup', {name: 'Show'}).getByRole('radio', {name: 'Logs'}),
  ).toBeChecked();
});
