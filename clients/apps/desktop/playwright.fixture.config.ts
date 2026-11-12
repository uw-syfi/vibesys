import {defineConfig} from '@playwright/test';
import {E2E_HOST, runPort} from '../../tools/e2e/free-port.mjs';

const inCI = !!process.env['CI'];
const port = await runPort('VIBESYS_E2E_DESKTOP_FIXTURE_PORT');

/** Browser verification of the built desktop fixture: semantic layout checks, never pixel baselines. */
export default defineConfig({
  testDir: './e2e/fixture',
  testMatch: '*.spec.ts',
  outputDir: './test-results/fixture',
  retries: 0,
  forbidOnly: inCI,
  workers: 1,
  // A hang guard only; the suite advances on app state, font readiness and animation completion.
  timeout: 120_000,
  expect: {timeout: 15_000},
  webServer: {
    command: `node e2e/fixture/serve-dist.mjs ${port}`,
    url: `http://${E2E_HOST}:${port}/fixture.html`,
    reuseExistingServer: false,
  },
  use: {
    baseURL: `http://${E2E_HOST}:${port}`,
    browserName: 'chromium',
    viewport: {width: 1280, height: 860},
    deviceScaleFactor: 1,
    colorScheme: 'light',
    contextOptions: {reducedMotion: 'reduce'},
    locale: 'en-US',
    timezoneId: 'UTC',
  },
});
