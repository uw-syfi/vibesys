import {defineConfig} from '@playwright/test';
import {E2E_HOST, runPort} from '../../tools/e2e/free-port.mjs';

const inCI = !!process.env['CI'];
const port = await runPort('VIBESYS_E2E_DESKTOP_RENDERER_PORT');

export default defineConfig({
  testDir: './e2e',
  retries: 0,
  forbidOnly: inCI,
  workers: 1,
  timeout: 30_000,
  webServer: {
    command: `pnpm exec vite dist --host ${E2E_HOST} --port ${port} --strictPort`,
    port,
    reuseExistingServer: false,
  },
  use: {baseURL: `http://${E2E_HOST}:${port}`, colorScheme: 'light'},
});
