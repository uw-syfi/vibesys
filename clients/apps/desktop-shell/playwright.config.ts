import {defineConfig} from '@playwright/test';
import {E2E_HOST, runPort} from '../../tools/e2e/free-port.mjs';
import {e2eTestMatch} from '../../tools/e2e/shard.mjs';
import {e2eWorkers} from '../../tools/e2e/workers.mjs';

/**
 * The app shell's journey: the built page against a Fake of the welcome preload's bridge
 * (`e2e/fake-bridge.ts`), so it runs in Chromium with no Electron and no host.
 *
 * Every test builds its own browser context and shares nothing with another, so the tests run on
 * `e2eWorkers()` workers, one test at a time on each. What a test leaves for the end of the run
 * goes to a file, not to a variable another worker cannot see: the `ledger` project runs after
 * every `screens` test has passed and reads what the screen ledgers wrote.
 */
const inCI = !!process.env['CI'];
const port = await runPort('VIBESYS_E2E_DESKTOP_SHELL_PORT');

export default defineConfig({
  testDir: './e2e',
  retries: 0,
  forbidOnly: inCI,
  workers: e2eWorkers(),
  fullyParallel: true,
  timeout: 30_000,
  projects: [
    {name: 'screens', testMatch: e2eTestMatch('desktop-shell')},
    {name: 'ledger', testMatch: '**/known-failures-final.ts', dependencies: ['screens']},
  ],
  webServer: {
    command: `node e2e/serve-dist.mjs ${port}`,
    port,
    reuseExistingServer: false,
  },
  use: {
    baseURL: `http://${E2E_HOST}:${port}`,
    viewport: {width: 1280, height: 860},
    colorScheme: 'light',
    contextOptions: {reducedMotion: 'reduce'},
  },
});
