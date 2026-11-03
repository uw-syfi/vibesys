/**
 * Drive a fiber whose work waits on both the virtual clock and real in-memory I/O (streams,
 * HTTP/2 over memory duplexes). Each turn lets pending I/O callbacks run, then moves the virtual
 * clock one `step`; the bound only guards against a fiber that never ends.
 */
import * as Effect from 'effect/Effect';
import * as Fiber from 'effect/Fiber';
import * as TestClock from 'effect/testing/TestClock';

const MOST_TURNS = 20_000;

/** One turn of the real event loop. */
const ioTurn: Effect.Effect<void> = Effect.promise(
  () => new Promise<void>(resolve => setImmediate(resolve)),
);

/** Wait, in real turns, until `done` holds. The test's own timeout bounds a `done` that never does. */
export const untilTrue = (done: () => boolean): Effect.Effect<void> =>
  Effect.gen(function* () {
    while (!done()) yield* ioTurn;
  });

/** Join `fiber`, moving the virtual clock by `step` between I/O turns. */
export const joinDriving = <A, E>(
  fiber: Fiber.Fiber<A, E>,
  step: `${number} millis` | `${number} second` | `${number} seconds` = '10 millis',
): Effect.Effect<A, E> =>
  Effect.gen(function* () {
    for (let turn = 0; turn < MOST_TURNS; turn += 1) {
      if (fiber.pollUnsafe() !== undefined) return yield* Fiber.join(fiber);
      yield* ioTurn;
      yield* TestClock.adjust(step);
    }
    return yield* Effect.die(new Error('the driven fiber never ended'));
  });
