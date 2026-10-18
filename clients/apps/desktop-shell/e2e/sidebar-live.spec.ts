import {mkdirSync} from 'node:fs';
import {expect, test} from '@playwright/test';
import {screenCheck} from './checks/screen-check.js';
import {installFakeBridge, installFakeDigests, installFakeMachines} from './fake-bridge.js';
import {SEED, sidebar} from './world.js';

/**
 * A live run's sidebar row says what the run has done so far (its ideas, the best result so far and
 * how long it has gone on), read from the same digest as a finished run's row, instead of only its
 * machine and phase. `SIDEBAR_LIVE_DIR` names a directory to save the sidebar's screenshot in.
 */

const LIVE_RUN = '20261015-163047';
const WORLD = {
  ...SEED,
  digests: {
    [LIVE_RUN]: {
      kind: 'read',
      createdAt: Date.UTC(2026, 9, 15, 16, 30),
      ideas: 9,
      adoptedRatio: null,
      bestSoFarRatio: 2.37,
      durationSeconds: 4920,
      outcome: null,
      closing: null,
      over: false,
    },
  },
};

const shots = process.env['SIDEBAR_LIVE_DIR'] || undefined;
if (shots !== undefined) mkdirSync(shots, {recursive: true});

test('a live run row shows its ideas, best so far and elapsed time', async ({page}) => {
  await page.setViewportSize({width: 1000, height: 700});
  await page.addInitScript(installFakeBridge, WORLD);
  await page.addInitScript(installFakeMachines, WORLD);
  await page.addInitScript(installFakeDigests, WORLD);
  await page.goto('./shell.html');
  const live = sidebar(page).locator('section[aria-label="Live"] .vs-sidebar-item');
  await expect(live).toHaveCount(1);
  await expect(live).toContainText('host-b · running · 9 ideas · 2.37× best so far · 1h 22m');
  expect(await screenCheck(page)).toEqual([]);
  if (shots !== undefined) {
    await sidebar(page).screenshot({path: `${shots}/sidebar-live.png`});
  }
});
