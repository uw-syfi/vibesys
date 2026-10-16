import {defineConfig} from '@playwright/test';

/**
 * The gallery's visual and accessibility suite (rules S11 and S12 of #2827).
 *
 * Screenshots are compared against baselines rendered by Chromium on Linux, the browser and OS the
 * `tui_e2e` CI job uses; the template has no platform suffix, so one set is checked in, and it is
 * written in the same Linux image (`pnpm update:screenshots`). The page draws its text only in the
 * bundled fonts, under reduced motion, at a fixed viewport and scale, with nothing from a clock.
 */
const inCI = !!process.env['CI'];

export default defineConfig({
  testDir: './e2e',
  retries: 0,
  forbidOnly: inCI,
  workers: 1,
  // A hang guard only: the suite takes well under a minute natively, three times that under the
  // x86-64 emulation that writes the baselines on an Arm Mac.
  timeout: 360_000,
  snapshotPathTemplate: '{testDir}/__screenshots__/{arg}{ext}',
  expect: {toHaveScreenshot: {animations: 'disabled', caret: 'hide', scale: 'css'}},
  webServer: {
    command: 'node e2e/serve-dist.mjs 5186',
    port: 5186,
    reuseExistingServer: !inCI,
  },
  use: {
    baseURL: 'http://127.0.0.1:5186',
    browserName: 'chromium',
    viewport: {width: 1280, height: 900},
    deviceScaleFactor: 1,
    colorScheme: 'light',
    contextOptions: {reducedMotion: 'reduce'},
    locale: 'en-US',
    timezoneId: 'UTC',
  },
});
