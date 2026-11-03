import {expect, it} from '@effect/vitest';
import * as Effect from 'effect/Effect';
import * as Fiber from 'effect/Fiber';
import {makeRetryGate} from './retry-gate.js';

// test-isolation: the gate's interleaving (a retry landing while the loop announces its stop) cannot
// be driven through a view, where nothing can run between the status change and the wait.
it.effect('a retry made while the loop announces that it stopped is not lost', () =>
  Effect.gen(function* () {
    const gate = yield* makeRetryGate;
    // The user clicks Retry the instant they see the status, which `announce` sets.
    yield* gate.wait(gate.open);
  }),
);

it.effect('releases the loop that waits, once, and forgets an open that nothing waited for', () =>
  Effect.gen(function* () {
    const gate = yield* makeRetryGate;
    yield* gate.open;
    const waiting = yield* Effect.forkChild(gate.wait(Effect.void));
    yield* Effect.yieldNow;
    expect(waiting.pollUnsafe()).toBeUndefined();
    yield* gate.open;
    yield* Fiber.join(waiting);
  }),
);
