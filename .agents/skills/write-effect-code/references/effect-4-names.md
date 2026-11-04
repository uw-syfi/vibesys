# Effect 4.1.2 names

Checked against the installed package. Recheck when the pinned version moves.

- Services: `class X extends Context.Service<X, Shape>()('app/X') {}`; `Layer.succeed(X, impl)`; the key string is the identity.
- Errors: `class E extends Data.TaggedError('E')<{readonly why: string}> {}`; `Effect.catchTag`, `Effect.flip` in tests.
- Callback to Effect: `Effect.callback<A, E>((resume, signal) => { resume(Effect.succeed(a)); return Effect.sync(cancel); })`. The returned Effect runs on interrupt, including `Effect.timeout`.
- Callback to Stream: `Stream.callback<A, E>(queue => Effect.gen(...), {bufferSize, strategy: 'dropping' | 'sliding' | 'suspend'})`. Inside, `Queue.offerUnsafe(queue, a)` returns `false` when the buffer refused it; end with `Queue.endUnsafe(queue)` or `Queue.failCauseUnsafe(queue, Cause.fail(e))`. The callback runs in a `Scope` (`Effect.addFinalizer`), which closes when the stream ends or its consumer is interrupted. `Queue.bounded(n)` also exists.
- Operations: `Effect.fn('Svc.op')(function* () {...})`, `Effect.fnUntraced` for a local helper. Children: `Effect.forkChild`, `Fiber.interrupt`, `Fiber.await`, `Fiber.join`.
- Time: `Effect.sleep`, `Effect.timeout`, `Schedule.exponential(base)`, `Schedule.jittered(schedule)`, `Schedule.modifyDelay`, `Effect.retry(effect, {schedule, times})`. `Duration.Input` accepts `'500 millis'`.
- Tests: `it.effect` (from `@effect/vitest`) runs on `TestClock` already; advance with `import * as TestClock from 'effect/testing/TestClock'` and `TestClock.adjust('1 second')`. `it.live` is the real clock (rejected by lint). `it.effect.prop` and `it.prop` exist for Schema/Arbitrary inputs; a plain `fast-check` property works inside `it.effect` when it only builds data.
