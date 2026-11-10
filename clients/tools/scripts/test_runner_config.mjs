import {fileURLToPath} from 'node:url';

/**
 * Shared hang guard. Tests advance fake clocks; elapsed time is never a verdict.
 * Loaded CI model tests can exceed Bun's 5 s default; 60 s leaves ample headroom.
 */
export const PER_TEST_BOUND_MS = 60_000;

/** Node reports the timed-out test's name and cancels it. Run files sequentially. */
export const NODE_TEST_ARGUMENTS = [
  '--test',
  `--test-timeout=${PER_TEST_BOUND_MS}`,
  '--test-concurrency=1',
];

/**
 * Vitest runs the Effect packages' tests (`@effect/vitest`), whose clock is `TestClock`. The same
 * hang guard as the other runners, one test file at a time. The first argument is the sub-command
 * and the rest are flags: nothing here changes which tests run.
 */
export const VITEST_ARGUMENTS = [
  'run',
  `--testTimeout=${PER_TEST_BOUND_MS}`,
  `--hookTimeout=${PER_TEST_BOUND_MS}`,
  '--no-file-parallelism',
];

/**
 * The file every test process loads first (Vitest `setupFiles`, bun `--preload`): it seeds
 * fast-check from `VIBESYS_PROPERTY_SEED` (see `property_seed.mjs`).
 */
export const PROPERTY_SEED_SETUP = fileURLToPath(
  new URL('./property_seed.setup.mjs', import.meta.url),
);
