import {defineConfig} from '@playwright/test';
import {E2E_HOST, runPort} from '../../tools/e2e/free-port.mjs';
import {e2eTestMatch} from '../../tools/e2e/shard.mjs';

/**
 * The app shell's journey: the built page against a Fake of the welcome preload's bridge
 * (`e2e/fake-bridge.ts`), so it runs in Chromium with no Electron and no host.
 */
const inCI = !!process.env['CI'];
const port = await runPort('VIBESYS_E2E_DESKTOP_SHELL_PORT');

export default defineConfig({
  testDir: './e2e',
  retries: 0,
  forbidOnly: inCI,
  workers: 1,
  fullyParallel: true,
  testMatch: e2eTestMatch('desktop-shell'),
  timeout: 30_000,
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
