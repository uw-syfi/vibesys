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
import {settled} from './checks/settle.js';
import {addInitScripts, initScript} from './init-scripts.js';
import {journeyScript} from './journey-world.js';

/**
 * The layout of the Agents face of the Run screen on the agents journey: the tree, an agent opened
 * beside it, a turn opened as a step, and the Usage face, each passing axe with a page that never
 * scrolls, in the Ink theme at 1440x900 and then 1000x900 on the same page. What each says (who
 * started whom, the tokens each agent and turn spent, the per-model split) is
 * `src/run-agents.dom.test.tsx`. `LEVEL_SHOTS` names a directory to save the screens in (the app's
 * main region only).
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

async function checkAt(page: Page, name: string): Promise<void> {
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

/**
 * Passes the checks in the state the page is in at 1440, and, for `narrow` (the states whose token
 * figures must fit beside the run's other facts), then at 1000, and returns to 1440.
 */
async function check(page: Page, name: string, narrow = false): Promise<void> {
  for (const [width, suffix] of [
    [1440, ''],
    ...(narrow ? ([[1000, '-1000']] as const) : []),
  ] as const) {
    await page.setViewportSize({width, height: 900});
    await settled(page);
    await checkAt(page, `${name}${suffix}`);
  }
  await page.setViewportSize({width: 1440, height: 900});
  await settled(page);
}

test.use({colorScheme: 'dark', viewport: {width: 1440, height: 900}});

test('the agents tree, an agent opened beside it and a turn opened as a step lay out', async ({
  page,
}) => {
  await openRun(page);
  await page.getByRole('radiogroup', {name: 'Show'}).getByText('Agents').click();
  const agents = page.getByRole('listbox', {name: 'Agents'});
  await expect(agents.getByRole('option')).toHaveCount(6);
  await check(page, 'ink-agents');

  await agents
    .getByRole('option', {name: /^Done Builder/})
    .first()
    .click();
  const turns = page.getByRole('listbox', {name: 'Turns'});
  await expect(turns.getByRole('option')).toHaveCount(1);
  await check(page, 'ink-agent');

  // A turn opens as a step, beside the agents; Escape returns to the agent.
  await turns.getByRole('option').first().click();
  await expect(page.getByRole('listbox', {name: 'Agents'})).toBeVisible();
  await expect(turns).toHaveCount(0);
  await page.keyboard.press('Escape');
  await expect(turns).toBeVisible();
});

test('the tokens each agent and turn name lay out beside the run’s other facts at 1440 and 1000', async ({
  page,
}) => {
  await openRun(page);
  await page.getByRole('radiogroup', {name: 'Show'}).getByText('Agents').click();
  const agents = page.getByRole('listbox', {name: 'Agents'});
  await expect(page.getByText('in (cached)', {exact: true})).toBeVisible();
  await check(page, 'ink-agents-tokens', true);
  await agents
    .getByRole('option', {name: /builder/i})
    .nth(1)
    .click();
  await check(page, 'ink-agent-tokens', true);
  await page.getByRole('listbox', {name: 'Turns'}).getByRole('option').first().click();
  await expect(page.getByText(/· in 48\.3k \(31k cached\) · out 2\.6k$/)).toBeVisible();
  await check(page, 'ink-turn-tokens', true);
});

test('the Usage face over turns with the per-model split lays out', async ({page}) => {
  await openRun(page);
  await page.getByRole('radiogroup', {name: 'Show'}).getByText('Usage').click();
  await expect(page.getByRole('table', {name: 'Usage by group'})).toBeVisible();
  await check(page, 'ink-usage-split');
});
