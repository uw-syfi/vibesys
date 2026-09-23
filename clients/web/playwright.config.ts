import {defineConfig} from '@playwright/test';

export default defineConfig({
  testDir: './e2e',
  webServer: {command: 'pnpm dev --host 127.0.0.1', port: 5173, reuseExistingServer: true},
  use: {baseURL: 'http://127.0.0.1:5173'},
});
