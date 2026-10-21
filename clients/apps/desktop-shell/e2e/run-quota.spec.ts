import AxeBuilder from '@axe-core/playwright';
import {expect, type Page, test} from '@playwright/test';
import {
  type FakeBridgeSeed,
  installFakeBridge,
  installFakeMachines,
  installFakeProjectRuns,
} from './fake-bridge.js';
import {installFakeRun} from './fake-run.js';
import {type FakeServerSeed, installFakeServer} from './fake-server.js';
import {addInitScripts, initScript} from './init-scripts.js';
import {STARTED_AT} from './started-at.js';

/**
 * The quota banner against the Fake bridge and a fake run paused on a usage limit: it names the
 * provider and its reset time, "Wait and resume when quota is back" sends Resume when capacity
 * returns and shows the expected time once the run reports `quota_waiting`, "Stop waiting" sends
 * Pause and the banner returns on `quota_wait_cancelled`, and "Resume now" sends Resume now. Times
 * are formatted in UTC here so the journey reads the same everywhere. Checked in the Ink theme;
 * `QUOTA_SHOTS` names a directory to save the screens in.
 */

interface FakeHostWindow extends Window {
  fakeHost: {commands: string[]};
}

const PROJECT = '/home/me/vibesys-projects/queue-rs';
const RUN_ID = '20261015-163047';
const INSTANCE = '00000000000a';
/** 2025-10-16 02:00 and 02:02 UTC. */
const RESETS_AT = 1_760_580_000;
const RESUMES_AT = 1_760_580_120;

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
  chromeInset: {width: 84, height: 40},
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

const SERVER: FakeServerSeed = {
  runId: RUN_ID,
  batch: {
    type: 'event_batch',
    events: [
      {
        timestamp: '2026-10-15T16:30:51Z',
        type: 'quota_paused',
        sequence: 1,
        data: {
          kind: 'quota_paused',
          provider: 'claude',
          condition: 'quota_exhausted',
          detail: 'Claude usage limit reached',
          resets_at: RESETS_AT,
        },
      },
    ],
    through_sequence: 1,
    store_id: 'store-1',
    history_after_sequence: 0,
  },
  snapshot: {run_id: RUN_ID, sequence: 0, status: 'paused'},
  quota: {provider: 'claude', resumesAt: RESUMES_AT},
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
  await addInitScripts(page, [
    initScript(installFakeBridge, SEED),
    initScript(installFakeProjectRuns, SEED),
    initScript(installFakeMachines, SEED),
    initScript(installFakeRun, {streams: [], text: {}}),
    initScript(installFakeServer, SERVER),
  ]);
  await page.goto('./shell.html');
  await page
    .getByRole('main')
    .getByRole('button', {name: /^queue-rs · mpmc/})
    .click();

  const main = page.getByRole('main');
  await expect(main.getByText('Paused · usage limit', {exact: true}).first()).toBeVisible();
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
  expect(await commands(page)).toEqual([]);
});
