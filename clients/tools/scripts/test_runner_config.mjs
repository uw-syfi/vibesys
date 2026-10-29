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
