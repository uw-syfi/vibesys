import {describe, expect, it} from '@effect/vitest';
import * as Effect from 'effect/Effect';
import * as Fiber from 'effect/Fiber';
import * as Ref from 'effect/Ref';
import * as TestClock from 'effect/testing/TestClock';
import {retryWithBackoff} from './backoff.js';

/** An operation that fails `failures` times and then succeeds, counting its attempts. */
const flaky = (attempts: Ref.Ref<number>, failures: number) =>
  Effect.gen(function* () {
    const attempt = yield* Ref.updateAndGet(attempts, count => count + 1);
    return attempt <= failures ? yield* Effect.fail(`attempt ${attempt} failed`) : attempt;
  });

describe('retryWithBackoff', () => {
  it.effect('waits base, then twice as long, on the virtual clock only', () =>
    Effect.gen(function* () {
      const attempts = yield* Ref.make(0);
      const fiber = yield* Effect.forkChild(
        retryWithBackoff(flaky(attempts, 2), {base: '100 millis', retries: 5}),
      );

      yield* TestClock.adjust('99 millis');
      expect(yield* Ref.get(attempts)).toBe(1);
      yield* TestClock.adjust('1 millis');
      expect(yield* Ref.get(attempts)).toBe(2);
      yield* TestClock.adjust('199 millis');
      expect(yield* Ref.get(attempts)).toBe(2);
      yield* TestClock.adjust('1 millis');

      expect(yield* Fiber.join(fiber)).toBe(3);
    }),
  );

  it.effect('fails with the last failure once the retries are spent', () =>
    Effect.gen(function* () {
      const attempts = yield* Ref.make(0);
      const fiber = yield* Effect.forkChild(
        retryWithBackoff(flaky(attempts, 10), {base: '1 second', retries: 2}),
      );

      yield* TestClock.adjust('3 seconds');

      expect(yield* Effect.flip(Fiber.join(fiber))).toBe('attempt 3 failed');
      expect(yield* Ref.get(attempts)).toBe(3);
    }),
  );
});
