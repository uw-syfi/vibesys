/** A place a stopped loop waits until the user asks it to try again. */
import * as Deferred from 'effect/Deferred';
import * as Effect from 'effect/Effect';
import * as Ref from 'effect/Ref';

export interface RetryGate {
  /**
   * Suspends until `open`. The gate is armed before `announce` runs, so a retry made the moment
   * the loop tells the user it stopped (`announce` sets the status they see) is not lost. An
   * `open` while nothing waits is not remembered.
   */
  readonly wait: (announce: Effect.Effect<void>) => Effect.Effect<void>;
  /** Releases the loop waiting at the gate, if any. */
  readonly open: Effect.Effect<void>;
}

export const makeRetryGate: Effect.Effect<RetryGate> = Effect.gen(function* () {
  const waiting = yield* Ref.make<Deferred.Deferred<void> | null>(null);
  const wait = (announce: Effect.Effect<void>) =>
    Effect.gen(function* () {
      const released = yield* Deferred.make<void>();
      yield* Ref.set(waiting, released);
      yield* announce;
      yield* Deferred.await(released);
    }).pipe(Effect.ensuring(Ref.set(waiting, null)));
  const open = Effect.gen(function* () {
    const released = yield* Ref.get(waiting);
    if (released !== null) yield* Deferred.succeed(released, undefined);
  });
  return {wait, open};
});
