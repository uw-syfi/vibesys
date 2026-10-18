import AxeBuilder from '@axe-core/playwright';
import {expect, type Page, test} from '@playwright/test';
import {type FakeBridgeSeed, installFakeBridge, installFakeMachines} from './fake-bridge.js';
import {installFakeRun} from './fake-run.js';
import {type FakeServerSeed, installFakeServer} from './fake-server.js';
import {judgeEvents, RUN_ID, traceEvents, turnEvents, usageEvents, W5_GATE} from './fake-trace.js';

/**
 * The Agents face of the Run screen against the Fake bridge and a fake run server whose span
 * events name each agent turn's agent and who started it: the tree of who started whom (with the
 * judge's fan-in), an agent opened beside the tree with its facts and turns, and a turn opened as
 * a step. The turns report what they spent (`turn_usage_recorded`): the run's total sits in the
 * header, each agent's in the tree and its pane, each turn's in its row and its pane header.
 * Checked in the Ink theme at 1440x900 and 1000x900; `LEVEL_SHOTS` names a directory to save the
 * screens in (the app's main region only).
 */

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
  return {kind: 'listed', host: 'ssh:host-b', live, ended, unreadable: [], unverified: []};
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

const SPAN_EVENTS = traceEvents();
const TURN_EVENTS = turnEvents(SPAN_EVENTS.length);
const JUDGE_EVENTS = judgeEvents(SPAN_EVENTS.length + TURN_EVENTS.length);
const EVENTS = [
  ...SPAN_EVENTS,
  ...TURN_EVENTS,
  ...JUDGE_EVENTS,
  ...usageEvents(SPAN_EVENTS.length + TURN_EVENTS.length + JUDGE_EVENTS.length),
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

test.use({colorScheme: 'dark', viewport: {width: 1440, height: 900}});

test('the agents tree shows who started whom, and an agent opens beside it with its turns', async ({
  page,
}) => {
  await openRun(page);
  await page.getByRole('radiogroup', {name: 'Show'}).getByText('Agents').click();
  const agents = page.getByRole('listbox', {name: 'Agents'});
  await expect(agents.getByRole('option')).toHaveCount(6);
  await expect(agents.getByRole('option').nth(0)).toContainText('planner');
  await expect(agents.getByRole('option', {name: /judge/})).toContainText(
    '+ also under implementer',
  );
  await expect(page.getByText('agent · started by the row above · now')).toBeVisible();
  await check(page, 'ink-agents');

  await agents
    .getByRole('option', {name: /^done implementer/})
    .first()
    .click();
  const turns = page.getByRole('listbox', {name: 'Turns'});
  await expect(turns.getByRole('option')).toHaveCount(1);
  await expect(page.getByRole('button', {name: 'planner'})).toBeVisible();
  await check(page, 'ink-agent');

  // A turn opens as a step, beside the agents.
  await turns.getByRole('option').first().click();
  await expect(page.getByRole('listbox', {name: 'Agents'})).toBeVisible();
  await expect(turns).toHaveCount(0);
  await page.keyboard.press('Escape');
  await expect(turns).toBeVisible();
});

test('the run, each agent and each turn name the tokens they spent', async ({page}) => {
  await openRun(page);
  // The run: every turn's input and output, 1,517,700 in all, exact on hover.
  const total = page.getByText('1.5M tokens');
  await expect(total).toBeVisible();
  await expect(total).toHaveAttribute(
    'title',
    'Input 1,466,600 (1,299,500 cached)\nOutput 51,100 (17,100 reasoning)\nTotal 1,517,700',
  );

  await page.getByRole('radiogroup', {name: 'Show'}).getByText('Agents').click();
  const agents = page.getByRole('listbox', {name: 'Agents'});
  await expect(page.getByText('tokens', {exact: true})).toBeVisible();
  // Each agent's turns summed: the first implementer's one turn, the planner's one measured turn.
  await expect(agents.getByRole('option', {name: /implementer/}).nth(0)).toContainText('1.2M');
  await expect(agents.getByRole('option', {name: /planner/}).nth(0)).toContainText('191.6k');
  await check(page, 'ink-agents-tokens');

  // W5's implementer: its pane totals it, and its turn opens with its tokens in the header.
  await agents
    .getByRole('option', {name: /implementer/})
    .nth(1)
    .click();
  await expect(page.getByText('50.9k', {exact: true}).first()).toBeVisible();
  await page.getByRole('listbox', {name: 'Turns'}).getByRole('option').first().click();
  await expect(page.getByText(/· 50\.9k tokens$/)).toBeVisible();
  await check(page, 'ink-turn-tokens');
});

test.describe('at 1000px', () => {
  test.use({viewport: {width: 1000, height: 900}});

  test('the token figures fit beside the run’s other facts', async ({page}) => {
    await openRun(page);
    await expect(page.getByText('1.5M tokens')).toBeVisible();
    await page.getByRole('radiogroup', {name: 'Show'}).getByText('Agents').click();
    const agents = page.getByRole('listbox', {name: 'Agents'});
    await expect(agents.getByRole('option', {name: /implementer/}).nth(0)).toContainText('1.2M');
    await check(page, 'ink-agents-tokens-1000');
    await agents
      .getByRole('option', {name: /implementer/})
      .nth(1)
      .click();
    await check(page, 'ink-agent-tokens-1000');
    await page.getByRole('listbox', {name: 'Turns'}).getByRole('option').first().click();
    await expect(page.getByText(/· 50\.9k tokens$/)).toBeVisible();
    await check(page, 'ink-turn-tokens-1000');
  });
});
