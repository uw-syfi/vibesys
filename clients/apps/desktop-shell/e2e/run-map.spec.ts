import AxeBuilder from '@axe-core/playwright';
import {expect, type Page, test} from '@playwright/test';
import {type FakeBridgeSeed, installFakeBridge, installFakeMachines} from './fake-bridge.js';
import {installFakeRun} from './fake-run.js';
import {type FakeServerSeed, installFakeServer} from './fake-server.js';
import {RUN_ID, traceEvents, turnEvents, W5_GATE} from './fake-trace.js';

/**
 * The Run screen's Map and time scrubber against the Fake bridge and a fake run server whose
 * stream carries a dynamic run's span events: the steps as a tree with finished work folded, the
 * state filter, a step's details and exactly its own logs, and the scrubber replaying the tree at
 * an earlier event, back to live, and on the ended run. Checked in the Ink theme; `MAP_SHOTS`
 * names a directory to save the screens in.
 */

interface FakeHostWindow extends Window {
  fakeHost: {listing(listing: unknown): void};
}

const PROJECT = '/home/me/vibesys-projects/queue-rs';
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
    startedAt: 1,
    reason: null,
  };
}

function listed(live: unknown[], ended: unknown[] = []): Record<string, unknown> {
  return {kind: 'listed', host: 'ssh:chelan4', live, ended, unreadable: [], unverified: []};
}

const SEED: FakeBridgeSeed = {
  theme: 'ink',
  chromeInset: {width: 84, height: 40},
  appearance: {mode: 'dark', light: 'paper', dark: 'ink'},
  listings: [listed([server('running')])],
  recent: [
    {
      host: 'ssh:chelan4',
      hostLabel: 'chelan4',
      project: PROJECT,
      task: 'mpmc',
      runId: RUN_ID,
      instanceId: INSTANCE,
      attachedAt: 3,
    },
  ],
};

const SPAN_EVENTS = traceEvents();
const EVENTS = [...SPAN_EVENTS, ...turnEvents(SPAN_EVENTS.length)];

const SERVER: FakeServerSeed = {
  runId: RUN_ID,
  batch: {
    type: 'event_batch',
    events: EVENTS,
    through_sequence: EVENTS.length,
    store_id: 'store-1',
    history_after_sequence: 0,
  },
  snapshot: {run_id: RUN_ID, sequence: 0, status: 'running'},
};

/** The W5 gate's benchmark stream names its span; the startup stream names none. */
const LOGS = {
  streams: [
    {
      id: 's1',
      owner: {kind: 'startup_step', stage: 'building_image'},
      command: 'docker build .',
      retained_start: 0,
      size: 0,
      outcome: 'ended',
    },
    {
      id: 's3',
      owner: {
        kind: 'gate_stage',
        execution_id: 'w5',
        gate: 'benchmark',
        attempt: 1,
        span_id: W5_GATE,
      },
      command: 'vibesys-queue benchmark',
      retained_start: 0,
      size: 0,
      outcome: null,
    },
  ],
  text: {
    s1: '#9 exporting to image\n',
    s3: 'vibesys-queue benchmark, 3 repetitions\nrepetition 1: 41.2 Mops/s\n',
  },
};

const shots = process.env['MAP_SHOTS'];

async function shot(page: Page, name: string): Promise<void> {
  if (shots !== undefined) await page.screenshot({path: `${shots}/${name}.png`, fullPage: true});
}

async function noViolations(page: Page): Promise<void> {
  expect((await new AxeBuilder({page}).analyze()).violations).toEqual([]);
}

/** The shell on the fake run, its Run screen open on the Map tab. */
async function openMap(page: Page): Promise<void> {
  await page.addInitScript(installFakeBridge, SEED);
  await page.addInitScript(installFakeMachines, SEED);
  await page.addInitScript(installFakeRun, LOGS);
  await page.addInitScript(installFakeServer, SERVER);
  await page.goto('./shell.html');
  await page
    .getByRole('navigation', {name: 'VibeSys'})
    .getByRole('button', {name: /^\S+ queue-rs · mpmc/})
    .click();
  await page.getByRole('tab', {name: 'Map'}).click();
}

test.use({colorScheme: 'dark'});

test('the Map shows the run as a tree, opens a step’s own logs, and replays it', async ({page}) => {
  await openMap(page);

  // The tree: run, the input's measurement, planning calls with finished work folded, live work.
  const tree = page.getByRole('tree', {name: 'Run steps'});
  const row = (name: string | RegExp) => tree.getByRole('treeitem', {name});
  await expect(row(/Run · dynamic/)).toBeVisible();
  await expect(row(/measure the input/)).toBeVisible();
  await expect(row(/4 finished workstreams/)).toHaveAttribute('aria-expanded', 'false');
  await expect(row(/W1 · /)).toHaveCount(0);
  await expect(row(/W5 · Combine W1 ring/)).toContainText('builds on W1');
  await expect(row(/Warm the build cache/)).toContainText('Cache warmup');
  const scrubber = page.getByRole('slider', {name: 'Run position'});
  const main = page.getByRole('main');
  await expect(main.getByText('Live', {exact: true})).toBeVisible();
  await noViolations(page);
  await shot(page, 'ink-map');

  // A step's details, and the links it builds on.
  await row(/4 finished workstreams/).click();
  await expect(row(/W1 · Lock-free ring/)).toBeVisible();
  await row(/W5 · Combine W1 ring/).click();
  const details = page.getByRole('region', {name: 'Workstream'});
  await expect(
    details.getByRole('heading', {name: 'W5 · Combine W1 ring with W2 padding'}),
  ).toBeVisible();
  await details.getByRole('button', {name: 'Builds on W1'}).click();
  await expect(details.getByText('Accepted').first()).toBeVisible();
  await expect(details.getByText('No logs for this step yet.')).toBeVisible();

  // The gate's logs are exactly the stream its owner names.
  await row(/Gate · correctness \+ benchmark/)
    .last()
    .click();
  const gate = page.getByRole('region', {name: 'Gate'});
  await expect(gate.getByRole('button')).toHaveText(['benchmark · w5 · attempt 1']);
  await noViolations(page);
  await shot(page, 'ink-map-step');
  await gate.getByRole('button', {name: 'benchmark · w5 · attempt 1'}).click();
  await expect(page.getByRole('tab', {name: 'Logs'})).toHaveAttribute('aria-selected', 'true');
  await expect(page.getByText('repetition 1: 41.2 Mops/s')).toBeVisible();
  await page.getByRole('tab', {name: 'Map'}).click();

  // The filter keeps what is in its state and the steps around it.
  await page.getByRole('radio', {name: 'Waiting'}).check();
  await expect(row(/W7 · Per-core queues/)).toBeVisible();
  await expect(row(/W6 · /)).toHaveCount(0);
  await page.getByRole('radio', {name: 'Finished'}).check();
  await expect(row(/W3 · Spin then park/)).toBeVisible();
  await expect(row(/finished workstreams/)).toHaveCount(0);
  await shot(page, 'ink-map-finished');
  await page.getByRole('radio', {name: 'All'}).check();

  // Replay: stepping back folds the trace up to an earlier event; Back to live returns.
  await scrubber.focus();
  await page.keyboard.press('Home');
  await expect(main.getByText('Replay', {exact: true})).toBeVisible();
  await expect(tree.getByRole('treeitem')).toHaveCount(0);
  for (let step = 0; step < 15; step += 1) await page.keyboard.press('ArrowRight');
  await expect(row(/Planning call 1/)).toBeVisible();
  await expect(row(/W1 · Lock-free ring/)).toContainText('Running');
  await expect(row(/Planning call 2/)).toHaveCount(0);
  await noViolations(page);
  await shot(page, 'ink-map-replay');
  await page.getByRole('button', {name: 'Back to live'}).click();
  await expect(main.getByText('Live', {exact: true})).toBeVisible();
  await expect(row(/Planning call 2/)).toBeVisible();

  // An ended run's scrubber reads its end, and jumps back to it.
  await page.evaluate(
    ([value]) => (window as unknown as FakeHostWindow).fakeHost.listing(value),
    [
      listed(
        [],
        [server('stopped', {outcome: 'stopped', reason: 'Stopped by you.', resumable: false})],
      ),
    ],
  );
  await expect(page.getByText('Stopped by you.')).toBeVisible();
  await expect(main.getByText('End', {exact: true})).toBeVisible();
  await scrubber.focus();
  await page.keyboard.press('ArrowLeft');
  await page.getByRole('button', {name: 'Jump to end'}).click();
  await expect(main.getByText('End', {exact: true})).toBeVisible();
  await shot(page, 'ink-map-ended');
});

test('a step picked far down the tree keeps its details and turns in view', async ({page}) => {
  await page.setViewportSize({width: 1280, height: 520});
  await openMap(page);
  const tree = page.getByRole('tree', {name: 'Run steps'});
  const row = (name: string | RegExp) => tree.getByRole('treeitem', {name});
  await row(/4 finished workstreams/).click();
  await row(/W5 · Combine W1 ring/).click();
  const details = page.getByRole('region', {name: 'Workstream'});
  const turns = page.getByRole('region', {name: 'Turn by turn'});
  await expect(turns.getByText('1 agent turn')).toBeVisible();

  // The tree is taller than the window; scrolled to its last row, the page has left the top of it.
  const last = tree.getByRole('treeitem').last();
  await last.scrollIntoViewIfNeeded();
  const scrolled = await tree.evaluate(element => {
    for (let at = element.parentElement; at !== null; at = at.parentElement) {
      if (at.scrollHeight > at.clientHeight && getComputedStyle(at).overflowY !== 'visible') {
        return at.scrollTop;
      }
    }
    return document.scrollingElement?.scrollTop ?? 0;
  });
  expect(scrolled).toBeGreaterThan(0);
  await expect(last).toBeInViewport();

  // The details stay beside it, and the turns with them.
  await expect(details).toBeInViewport();
  await expect(turns).toBeInViewport();
});

test('the Map shows the turns under a step, turn by turn, and none for a step without any', async ({
  page,
}) => {
  await openMap(page);
  const tree = page.getByRole('tree', {name: 'Run steps'});
  const row = (name: string | RegExp) => tree.getByRole('treeitem', {name});
  await row(/W5 · Combine W1 ring/).click();
  const turns = page.getByRole('region', {name: 'Turn by turn'});
  await expect(turns.getByText('1 agent turn')).toBeVisible();
  const list = turns.getByRole('list', {name: 'Turn by turn'});
  await expect(
    list.getByText('I will pad each counter to its own line and re-measure.'),
  ).toBeVisible();

  // The reasoning starts closed; a tool call opens to its input and its result.
  const reasoning = list.getByRole('button', {name: /Reasoning/});
  await expect(reasoning).toHaveAttribute('aria-expanded', 'false');
  await reasoning.click();
  await expect(list.getByText('The head and tail counters share a cache line.')).toBeVisible();
  const bash = list.getByRole('button', {name: /Bash/});
  await expect(bash).toHaveAttribute('aria-expanded', 'false');
  await bash.click();
  await expect(list.getByText('"command": "cargo bench --bench queue"')).toBeVisible();
  await expect(list.getByText('queue/mpmc time: 41.2 ms')).toBeVisible();
  await noViolations(page);
  await shot(page, 'ink-map-turns');

  // Another workstream has no turn with a transcript, and says so.
  await row(/W6 · Wait-free consumer path/).click();
  await expect(turns.getByText('No agent turns for this step')).toBeVisible();
  await noViolations(page);
});
