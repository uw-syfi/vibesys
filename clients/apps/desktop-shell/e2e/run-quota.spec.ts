import AxeBuilder from '@axe-core/playwright';
import {expect, type Page, test} from '@playwright/test';
import {Controls} from './chat-controls.js';
import {
  type FakeBridgeSeed,
  installFakeBridge,
  installFakeMachines,
  installFakeProjectRuns,
  MAC_CHROME,
} from './fake-bridge.js';
import {addInitScripts, initScript} from './init-scripts.js';
import {journeyFixture, journeyScript} from './journey-world.js';
import {installGatewayOnly} from './retired-run-bridge.js';
import {STARTED_AT} from './started-at.js';

/** Quota holds stay internal; Pause now remains available to close the epoch. */

interface FakeHostWindow extends Window {
  fakeHost: {commands: string[]};
}

const PROJECT = '/home/me/vibesys-projects/queue-rs';
const RUN_ID = '20261015-163047';
const INSTANCE = '00000000000a';
const LIVE = {
  id: INSTANCE,
  projectRoot: PROJECT,
  runId: RUN_ID,
  status: 'paused',
  phase: 'paused',
  paused: 'quota',
  end: null,
  startedAt: STARTED_AT,
  reason: null,
};

const SEED: FakeBridgeSeed = {
  theme: 'ink',
  chromeInset: MAC_CHROME,
  appearance: {mode: 'dark', light: 'paper', dark: 'ink'},
  listings: [
    {kind: 'listed', host: 'ssh:host-b', live: [LIVE], ended: [], unreadable: [], unverified: []},
  ],
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

const shots = process.env['QUOTA_SHOTS'];

async function commands(page: Page): Promise<string[]> {
  return page.evaluate(() => [...(window as unknown as FakeHostWindow).fakeHost.commands]);
}

async function shoot(page: Page, name: string): Promise<void> {
  if (shots !== undefined) await page.screenshot({path: `${shots}/${name}.png`});
}

test.use({colorScheme: 'dark', timezoneId: 'UTC', locale: 'en-US'});

test('a quota hold stays internal while the operator can cancel the epoch', async ({page}) => {
  const controls = new Controls(page, journeyFixture('quota'));
  await controls.attach();
  await addInitScripts(page, [
    initScript(installFakeBridge, SEED),
    initScript(installFakeProjectRuns, SEED),
    initScript(installFakeMachines, SEED),
    initScript(installGatewayOnly, undefined),
    journeyScript('quota'),
  ]);
  await page.goto('./shell.html');
  await page
    .getByRole('main')
    .getByRole('button', {name: /^queue-rs · mpmc/})
    .click();

  const main = page.getByRole('main');
  // Capacity waits are native child spans; they do not park the epoch.
  await expect(main.getByText('Running', {exact: true}).first()).toBeVisible();
  await expect(page.getByRole('button', {name: 'Wait and resume when quota is back'})).toHaveCount(
    0,
  );
  await expect(page.getByRole('button', {name: 'Stop waiting'})).toHaveCount(0);
  await expect(page.getByRole('button', {name: 'Resume now'})).toHaveCount(0);
  await page.getByRole('button', {name: 'Pause now', exact: true}).click();
  await expect(
    page.getByText('Unfinished work may need repeating and may still incur charges.'),
  ).toBeVisible();
  expect((await new AxeBuilder({page}).analyze()).violations).toEqual([]);
  await shoot(page, 'ink-quota-held');
  expect((await commands(page)).filter(command => command.startsWith('command.'))).toEqual([]);
  // The warning stands beside the controls and Pause now is admitted at once: no dialog precedes it.
  expect(controls.posted.map(request => request.intent.case)).toEqual(['pauseNow']);
});
