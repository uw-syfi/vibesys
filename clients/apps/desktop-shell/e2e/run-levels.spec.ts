import AxeBuilder from '@axe-core/playwright';
import {expect, type Locator, type Page, test} from '@playwright/test';
import {type FakeBridgeSeed, installFakeBridge, installFakeMachines} from './fake-bridge.js';
import {installFakeRun} from './fake-run.js';
import {type FakeServerSeed, installFakeServer} from './fake-server.js';
import {judgeEvents, RUN_ID, traceEvents, turnEvents, W5_GATE} from './fake-trace.js';

/**
 * The Run screen's levels against the Fake bridge and a fake run server whose stream carries a
 * dynamic run's span events and W5's turn: the run level (the hero, the ideas, Now), an idea, a
 * step and a step's item, reached by click, walked back by Escape, the breadcrumb and the
 * browser's Back, with no page scroll at any level. Checked in the Ink theme at 1440×900;
 * `LEVEL_SHOTS` names a directory to save the screens in (the app's main region only).
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
const TURN_EVENTS = turnEvents(SPAN_EVENTS.length);
const EVENTS = [
  ...SPAN_EVENTS,
  ...TURN_EVENTS,
  ...judgeEvents(SPAN_EVENTS.length + TURN_EVENTS.length),
];

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

const LOGS = {
  streams: [
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
  text: {s3: 'vibesys-queue benchmark, 3 repetitions\nrepetition 1: 41.2 Mops/s\n'},
};

const shots = process.env['LEVEL_SHOTS'];

async function check(page: Page, name: string): Promise<void> {
  expect((await new AxeBuilder({page}).analyze()).violations).toEqual([]);
  // The page is fixed: whatever the level, it never scrolls; its panes do.
  expect(
    await page.evaluate(() => document.documentElement.scrollHeight <= window.innerHeight),
  ).toBe(true);
  if (shots !== undefined) await page.getByRole('main').screenshot({path: `${shots}/${name}.png`});
}

async function openRun(page: Page): Promise<void> {
  await page.addInitScript(installFakeBridge, SEED);
  await page.addInitScript(installFakeMachines, SEED);
  await page.addInitScript(installFakeRun, LOGS);
  await page.addInitScript(installFakeServer, SERVER);
  await page.goto('./shell.html');
  await page
    .getByRole('navigation', {name: 'VibeSys'})
    .getByRole('button', {name: /^\S+ queue-rs · mpmc/})
    .click();
}

/** Presses Tab until the focus is inside `target`; at most `limit` presses (guards a hang). */
async function tabInto(page: Page, target: Locator, limit = 60): Promise<void> {
  for (let press = 0; press < limit; press += 1) {
    if (await target.evaluate(element => element.contains(document.activeElement))) return;
    await page.keyboard.press('Tab');
  }
  throw new Error(`Tab never reached ${String(target)}`);
}

test.use({colorScheme: 'dark', viewport: {width: 1440, height: 900}});

test('every level opens from the keyboard alone, and Escape walks back', async ({page}) => {
  await openRun(page);
  const ideas = page.getByRole('listbox', {name: 'Ideas'});
  await expect(ideas.getByRole('option').first()).toBeVisible();
  await tabInto(page, ideas);
  // Down to the third row, then Enter opens it.
  await page.keyboard.press('ArrowDown');
  await page.keyboard.press('ArrowDown');
  await page.keyboard.press('Enter');
  await expect(page.getByRole('heading', {name: 'Combine W1 ring with W2 padding'})).toBeVisible();

  const steps = page.getByRole('listbox', {name: /steps$/});
  await tabInto(page, steps);
  await page.keyboard.press('Home');
  const implementer = steps.getByRole('option', {name: /implementer/}).first();
  for (let press = 0; press < 20; press += 1) {
    if (await implementer.evaluate(element => element === document.activeElement)) break;
    await page.keyboard.press('ArrowDown');
  }
  await page.keyboard.press('Enter');
  const tools = page.getByRole('listbox', {name: 'Tool calls'});
  await expect(tools).toBeVisible();

  await tabInto(page, tools);
  const bench = tools.getByRole('option', {name: /cargo bench --bench queue/});
  await page.keyboard.press('Home');
  for (let press = 0; press < 40; press += 1) {
    if (await bench.evaluate(element => element === document.activeElement)) break;
    await page.keyboard.press('ArrowDown');
  }
  await page.keyboard.press('Enter');
  await expect(page.getByText('queue/mpmc time: 41.2 ms')).toBeVisible();

  for (const _level of ['step', 'idea', 'run']) await page.keyboard.press('Escape');
  await expect(page.getByRole('listbox', {name: 'Running now'})).toBeVisible();
  await expect(page.getByRole('heading', {name: 'Combine W1 ring with W2 padding'})).toHaveCount(0);
});

test('the run level shows the best ratio, the ideas running first, and what runs now', async ({
  page,
}) => {
  await openRun(page);
  const ideas = page.getByRole('listbox', {name: 'Ideas'});
  // Running first, then the kept newest first; the rejected wait behind their row.
  await expect(ideas.getByRole('option')).toHaveText([
    /Per-core queues with stealing/,
    /Wait-free consumer path/,
    /Combine W1 ring with W2 padding/,
    /Cache-line padding and batched enqueue/,
    /Lock-free ring with per-slot sequence numbers/,
  ]);
  await expect(page.getByText('1.18×').first()).toBeVisible();
  const now = page.getByRole('listbox', {name: 'Running now'});
  await expect(now.getByRole('option', {name: /Combine W1 ring/}).first()).toBeVisible();
  await check(page, 'ink-run');

  // The rejected join the list from their row, and the search narrows it.
  await page.getByRole('button', {name: '+ 2 rejected'}).click();
  await expect(ideas.getByRole('option', {name: /Spin then park/})).toBeVisible();
  await page.getByLabel('Search ideas').fill('spin');
  await expect(ideas.getByRole('option')).toHaveText([/Spin then park/]);

  // The mode switch keeps the filter in the page state.
  const show = page.getByRole('radiogroup', {name: 'Show'});
  await show.getByText('Agents').click();
  await show.getByText('Ideas').click();
  await expect(page.getByLabel('Search ideas')).toHaveValue('spin');
});

test('every level opens by click and walks back by Escape, the crumbs and Back', async ({page}) => {
  await openRun(page);
  const ideas = page.getByRole('listbox', {name: 'Ideas'});

  // Level 1: the idea beside the ideas, with what it built on.
  await ideas.getByRole('option', {name: /^running Combine W1 ring with W2 padding/}).click();
  await expect(page.getByRole('heading', {name: 'Combine W1 ring with W2 padding'})).toBeVisible();
  await expect(
    page.getByRole('button', {name: 'Lock-free ring with per-slot sequence numbers'}),
  ).toBeVisible();
  await check(page, 'ink-idea');

  // Level 2: a step beside the idea's steps.
  const steps = page.getByRole('listbox', {name: /steps$/});
  await steps
    .getByRole('option', {name: /implementer/})
    .first()
    .click();
  const tools = page.getByRole('listbox', {name: 'Tool calls'});
  await expect(tools.getByRole('option', {name: /cargo bench --bench queue/})).toBeVisible();
  await check(page, 'ink-step');

  // Level 3: the item in full, beside its step.
  await tools.getByRole('option', {name: /cargo bench --bench queue/}).click();
  await expect(page.getByText('queue/mpmc time: 41.2 ms')).toBeVisible();
  await check(page, 'ink-item');

  // Escape goes up a level, Back retraces the zooms, the breadcrumb jumps to the run.
  await page.keyboard.press('Escape');
  await expect(page.getByText('queue/mpmc time: 41.2 ms')).toHaveCount(0);
  await expect(tools).toBeVisible();
  // Escape is a zoom of its own: Back returns to the item, Forward to the step again.
  await page.goBack();
  await expect(page.getByText('queue/mpmc time: 41.2 ms')).toBeVisible();
  await page.goForward();
  await expect(page.getByText('queue/mpmc time: 41.2 ms')).toHaveCount(0);
  await expect(tools).toBeVisible();
  await page.getByRole('navigation', {name: 'Zoom'}).getByRole('button', {name: 'run'}).click();
  await expect(page.getByRole('listbox', {name: 'Running now'})).toBeVisible();

  // Now jumps straight to the live step; Escape walks up to the run.
  await page
    .getByRole('listbox', {name: 'Running now'})
    .getByRole('option', {name: /Combine W1 ring/})
    .first()
    .click();
  await expect(page.getByRole('heading', {name: 'correctness + benchmark'})).toBeVisible();
  await page.keyboard.press('Escape');
  await page.keyboard.press('Escape');
  await expect(page.getByRole('listbox', {name: 'Running now'})).toBeVisible();
});

test('the scrubber replays the run’s ideas at an earlier event and returns to live', async ({
  page,
}) => {
  await openRun(page);
  const ideas = page.getByRole('listbox', {name: 'Ideas'});
  await expect(ideas.getByRole('option')).toHaveCount(5);
  await page.getByRole('slider', {name: 'Run position'}).focus();
  await page.keyboard.press('Home');
  await expect(page.getByText('No ideas yet')).toBeVisible();
  await page.getByRole('button', {name: 'Back to live'}).click();
  await expect(ideas.getByRole('option')).toHaveCount(5);
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
  await check(page, 'ink-run-ended');
});
