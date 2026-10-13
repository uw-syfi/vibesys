import {defineConfig} from '@playwright/test';

const inCI = !!process.env['CI'];

export default defineConfig({
  testDir: './e2e',
  retries: 0,
  forbidOnly: inCI,
  workers: 1,
  timeout: 30_000,
});
