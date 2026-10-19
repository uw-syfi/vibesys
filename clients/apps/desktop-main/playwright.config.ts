import {defineConfig} from '@playwright/test';
import {e2eShard} from '../../tools/e2e/shard.mjs';

const inCI = !!process.env['CI'];

export default defineConfig({
  testDir: './e2e',
  retries: 0,
  forbidOnly: inCI,
  workers: 1,
  // CI runs one slice of the specs per job (VIBESYS_E2E_SHARD, "2/5"); unset runs them all.
  shard: e2eShard('VIBESYS_E2E_SHARD') ?? null,
  timeout: 30_000,
});
