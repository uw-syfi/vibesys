/**
 * Quiescence: the point where every fiber of the code under test is waiting on something a test
 * controls (the virtual clock, a call the fake holds open, a gate), so what a screen would read
 * is final for this virtual instant.
 *
 * It is observed, not guessed. The scheduler the code under test runs on counts the tasks it has
 * queued and not yet started; `settle` yields until none is queued. A frame that crosses any
 * number of fibers keeps a task queued until it arrives, so no fixed count of turns is needed.
 * A fiber only counts when it was started under that scheduler, so build the code under test
 * from `makeQuiescence` (the host harness does).
 */
import * as Effect from 'effect/Effect';
import * as Scheduler from 'effect/Scheduler';

/** Turns `settle` may take before it gives up: a bound against a fiber that never stops scheduling. */
const MOST_TURNS = 100_000;

export interface Quiescence {
  /** The scheduler to run the code under test on. */
  readonly scheduler: Scheduler.Scheduler;
  /** Suspends until no task of that scheduler is queued. Never advances the clock. */
  readonly settle: Effect.Effect<void>;
}

/** The `settle` of each counting scheduler, found from the scheduler a fiber runs on. */
const settlers = new WeakMap<Scheduler.Scheduler, Effect.Effect<void>>();

/** A scheduler that counts queued tasks, over the one the test already runs on (the test clock's). */
export const makeQuiescence: Effect.Effect<Quiescence> = Effect.gen(function* () {
  const parent = yield* Scheduler.Scheduler;
  let queued = 0;
  const scheduler: Scheduler.Scheduler = {
    executionMode: parent.executionMode,
    shouldYield: fiber => parent.shouldYield(fiber),
    makeDispatcher() {
      const dispatcher = parent.makeDispatcher();
      return {
        scheduleTask(task, priority) {
          queued += 1;
          dispatcher.scheduleTask(() => {
            queued -= 1;
            task();
          }, priority);
        },
        flush: () => dispatcher.flush(),
      };
    },
  };
  const settle: Effect.Effect<void> = Effect.gen(function* () {
    // One turn first: a fiber the test just forked has queued its start on the test's own scheduler.
    for (let turn = 0; turn < MOST_TURNS; turn += 1) {
      yield* Effect.yieldNow;
      if (queued === 0) return;
    }
    return yield* Effect.die(new Error('the fibers under test never stopped scheduling work'));
  });
  settlers.set(scheduler, settle);
  return {scheduler, settle};
});

/** Run `self` with every fiber it starts on a counting scheduler, so `settle` inside it is exact. */
export const quiescent = <A, E, R>(self: Effect.Effect<A, E, R>): Effect.Effect<A, E, R> =>
  Effect.flatMap(makeQuiescence, quiet =>
    self.pipe(Effect.provideService(Scheduler.Scheduler, quiet.scheduler)),
  );

/** Suspends until the fibers of the enclosing `quiescent` body have nothing left to run. */
export const settle: Effect.Effect<void> = Effect.flatMap(
  Scheduler.Scheduler,
  scheduler =>
    settlers.get(scheduler) ??
    Effect.die(new Error('settle needs the test body to run under quiescent(...)')),
);
