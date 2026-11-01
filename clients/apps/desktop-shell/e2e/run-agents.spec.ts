import AxeBuilder from '@axe-core/playwright';
import {expect, type Page, test} from '@playwright/test';
import {
  type FakeBridgeSeed,
  installFakeBridge,
  installFakeChrome,
  installFakeMachines,
  installFakeProjectRuns,
  MAC_CHROME,
} from '../src/testing/fake-bridge.js';
import {STARTED_AT} from '../src/testing/started-at.js';
import {addInitScripts, initScript} from './init-scripts.js';
import {journeyScript} from './journey-world.js';

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

function listed(live: unknown[], ended: unknown[] = []): Record<string, unknown> {
  return {kind: 'listed', host: 'ssh:host-b', live, ended, unreadable: [], unverified: []};
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
  await addInitScripts(page, [
    initScript(installFakeChrome, SEED),
    initScript(installFakeBridge, SEED),
    initScript(installFakeProjectRuns, SEED),
    initScript(installFakeMachines, SEED),
    journeyScript('agents'),
  ]);
  await page.goto('./shell.html');
  await page
    .getByRole('main')
    .getByRole('button', {name: /^queue-rs · mpmc/})
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
  await expect(agents.getByRole('option').nth(0)).toContainText('Planner');
  await expect(agents.getByRole('option', {name: /reviewer/i})).toContainText(
    'also started by Builder',
  );
  await expect(page.getByText('agent · what it is doing')).toBeVisible();
  await check(page, 'ink-agents');

  await agents
    .getByRole('option', {name: /^Done Builder/})
    .first()
    .click();
  const turns = page.getByRole('listbox', {name: 'Turns'});
  await expect(turns.getByRole('option')).toHaveCount(1);
  await expect(page.getByRole('button', {name: 'Planner'})).toBeVisible();
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
  // The run: every turn's input (with its cached share) and output, exact on hover.
  const total = page.locator('.vs-stat', {hasText: 'Tokens'});
  await expect(total).toContainText(/1\.5M input\s*· 1\.3M cached · 51\.1k output/);
  await expect(total).toHaveAttribute(
    'title',
    'Input 1,466,600 (1,299,500 cached, 167,100 uncached)\nOutput 51,100 (17,100 reasoning)\nTotal 1,517,700',
  );

  await page.getByRole('radiogroup', {name: 'Show'}).getByText('Agents').click();
  const agents = page.getByRole('listbox', {name: 'Agents'});
  await expect(page.getByText('in (cached)', {exact: true})).toBeVisible();
  // Each agent's turns summed: the first implementer's one turn, the planner's one measured turn,
  // input with its cached share beside it, then output.
  await expect(agents.getByRole('option', {name: /builder/i}).nth(0)).toContainText(
    '1.2M 1.1M cached',
  );
  await expect(agents.getByRole('option', {name: /planner/i}).nth(0)).toContainText('182.4k');
  await check(page, 'ink-agents-tokens');

  // W5's implementer: its pane totals it, and its turn opens with its tokens in the header.
  await agents
    .getByRole('option', {name: /builder/i})
    .nth(1)
    .click();
  await expect(page.getByText('in 48.3k (31k cached) · out 2.6k').first()).toBeVisible();
  await page.getByRole('listbox', {name: 'Turns'}).getByRole('option').first().click();
  await expect(page.getByText(/· in 48\.3k \(31k cached\) · out 2\.6k$/)).toBeVisible();
  await check(page, 'ink-turn-tokens');
});

test('the Usage face reads turns that carry the per-model split, and the split moves no figure', async ({
  page,
}) => {
  // The planner's turn ran a model the price table does not know, so it has no cost; the
  // judge's turn ran on gpt-6.1-sol and its cost is an amount. Both lines carry `by_model`,
  // `cost_basis` and `provider_session_id`; the other turns' lines do not.
  await openRun(page);
  await page.getByRole('radiogroup', {name: 'Show'}).getByText('Usage').click();
  const table = page.getByRole('table', {name: 'Usage by group'});
  await expect(table).toBeVisible();
  const cost = (group: string) =>
    table.locator('tbody tr', {hasText: group}).first().locator('td').nth(6);
  await expect(cost('Reviewer')).toHaveText('$0.02');
  await expect(cost('Planner')).toHaveText('unknown');
  await expect(cost('Builder')).toHaveText('unknown');
  await expect(page.getByRole('region', {name: 'Totals'})).toContainText(
    'in 1.5M · out 34k · reasoning 17.1k',
  );
  await check(page, 'ink-usage-split');
});

test.describe('at 1000px', () => {
  test.use({viewport: {width: 1000, height: 900}});

  test('the token figures fit beside the run’s other facts', async ({page}) => {
    await openRun(page);
    await expect(page.locator('.vs-stat', {hasText: 'Tokens'})).toContainText(
      /1\.5M input\s*· 1\.3M cached · 51\.1k output/,
    );
    await page.getByRole('radiogroup', {name: 'Show'}).getByText('Agents').click();
    const agents = page.getByRole('listbox', {name: 'Agents'});
    await expect(agents.getByRole('option', {name: /builder/i}).nth(0)).toContainText('1.2M');
    await check(page, 'ink-agents-tokens-1000');
    await agents
      .getByRole('option', {name: /builder/i})
      .nth(1)
      .click();
    await check(page, 'ink-agent-tokens-1000');
    await page.getByRole('listbox', {name: 'Turns'}).getByRole('option').first().click();
    await expect(page.getByText(/· in 48\.3k \(31k cached\) · out 2\.6k$/)).toBeVisible();
    await check(page, 'ink-turn-tokens-1000');
  });
});
