/** The window's key presses, pushed by a test. Presses wait for the listener, so none is lost. */
import * as Effect from 'effect/Effect';
import * as Layer from 'effect/Layer';
import * as Queue from 'effect/Queue';
import * as Stream from 'effect/Stream';
import {KeyPresses} from '../../core/shell/navigation.js';
import type {KeyPress} from '../../state/index.js';

export interface FakeKeyPresses {
  readonly layer: Layer.Layer<KeyPresses>;
  readonly press: (press: KeyPress) => Effect.Effect<void>;
}

export const makeFakeKeyPresses: Effect.Effect<FakeKeyPresses> = Effect.gen(function* () {
  const queue = yield* Queue.unbounded<KeyPress>();
  return {
    layer: Layer.succeed(KeyPresses, KeyPresses.of({presses: Stream.fromQueue(queue)})),
    press: press => Queue.offer(queue, press).pipe(Effect.asVoid),
  };
});
