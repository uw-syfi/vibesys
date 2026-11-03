import * as Effect from 'effect/Effect';

/** How many times a fiber yields so that fibers it woke, and the ones those woke, have each had a turn. */
const SETTLE_TURNS = 64;

/**
 * Give every other runnable fiber its turn at the current virtual time. A forked fiber under
 * `TestClock` runs only when the test yields, and a frame crosses several fibers before a
 * consumer sees it; this yields past all of them. It never advances the clock.
 */
export const settle: Effect.Effect<void> = Effect.gen(function* () {
  for (let turn = 0; turn < SETTLE_TURNS; turn += 1) yield* Effect.yieldNow;
});
