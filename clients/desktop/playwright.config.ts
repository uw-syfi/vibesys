import {defineConfig} from '@playwright/test';

const inCI = !!process.env['CI'];

export default defineConfig({
  testDir: './e2e',
  retries: 0,
  forbidOnly: inCI,
  workers: 1,
  timeout: 30_000,
  webServer: {
    command: 'pnpm --dir .. exec vite desktop/dist/ui --host 127.0.0.1 --port 5184 --strictPort',
    port: 5184,
    reuseExistingServer: !inCI,
  },
  use: {baseURL: 'http://127.0.0.1:5184', colorScheme: 'light'},
});
