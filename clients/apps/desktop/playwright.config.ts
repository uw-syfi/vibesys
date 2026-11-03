import {defineConfig} from '@playwright/test';

/**
 * The observer of the desktop path end to end test (`gateway/e2e`, run with
 * `VIBESYS_E2E_DESKTOP_UI=1`): one Playwright test that launches the built app against the world
 * that test started. The file is named without `.spec` on purpose. A `.spec` file joins the
 * duration-weighted plan the `clients-e2e` shards split (`tools/e2e/shard.mts`), where this one
 * has no world to attach to; this one runs only in the `desktop-path` job, driven by its conductor.
 */
const inCI = !!process.env['CI'];

export default defineConfig({
  testDir: './e2e',
  testMatch: 'observe-run.ts',
  retries: 0,
  forbidOnly: inCI,
  workers: 1,
  // A bound that only guards against a hang; every wait inside is an expectation with its own.
  timeout: 90_000,
  expect: {timeout: 30_000},
});
