import type * as Duration from 'effect/Duration';
import * as Effect from 'effect/Effect';
import * as Schedule from 'effect/Schedule';

/** How a dropped operation is tried again: waits `base`, doubling, for at most `retries` retries. */
export interface BackoffPolicy {
  readonly base: Duration.Input;
  readonly retries: number;
}

/**
 * Run `operation`, and after each typed failure wait twice as long as before and run it again,
 * until it succeeds or `retries` retries are spent; then fail with the last failure. Waiting is
 * the ambient `Clock`, so a test advances it with `TestClock` and never sleeps.
 */
export const retryWithBackoff = <A, E, R>(
  operation: Effect.Effect<A, E, R>,
  {base, retries}: BackoffPolicy,
): Effect.Effect<A, E, R> =>
  Effect.retry(operation, {schedule: Schedule.exponential(base), times: retries});
