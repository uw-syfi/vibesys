import {defineConfig} from '@playwright/test';
import {e2eTestMatch} from '../../tools/e2e/shard.mjs';

const inCI = !!process.env['CI'];

export default defineConfig({
  testDir: './e2e',
  retries: 0,
  forbidOnly: inCI,
  workers: 1,
  testMatch: e2eTestMatch('desktop-main'),
  timeout: 30_000,
});
