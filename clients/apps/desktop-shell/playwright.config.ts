import {defineConfig} from '@playwright/test';

/**
 * The app shell's journey: the built page against a Fake of the welcome preload's bridge
 * (`e2e/fake-bridge.ts`), so it runs in Chromium with no Electron and no host.
 */
const inCI = !!process.env['CI'];

export default defineConfig({
  testDir: './e2e',
  retries: 0,
  forbidOnly: inCI,
  workers: 1,
  timeout: 30_000,
  webServer: {
    command: 'pnpm exec vite preview --host 127.0.0.1 --port 5187 --strictPort',
    port: 5187,
    reuseExistingServer: !inCI,
  },
  use: {
    baseURL: 'http://127.0.0.1:5187',
    viewport: {width: 1280, height: 860},
    colorScheme: 'light',
    contextOptions: {reducedMotion: 'reduce'},
  },
});
