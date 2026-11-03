/**
 * The contract every `GatewayTransport` passes: what a call delivers, how it fails, and that its
 * resources are released exactly once however it ends, including when the caller gives up. The
 * real transport (over a fake bridge) and the fake transport both run it, so the fake cannot be
 * more forgiving than the real one. A test file registers an implementation with
 * `gatewayTransportContract`.
 */
import {describe, expect, it} from '@effect/vitest';
import * as Cause from 'effect/Cause';
import * as Effect from 'effect/Effect';
import * as Exit from 'effect/Exit';
import * as Fiber from 'effect/Fiber';
import type * as Layer from 'effect/Layer';
import * as Schema from 'effect/Schema';
import * as Stream from 'effect/Stream';
import {GatewayTransport} from '../../core/transport/gateway-transport.js';
import {settle} from '../settle.js';
import type {Recording, Step} from './fake-transport.js';

/** An implementation under test, playing `recording`. */
interface TransportWorld {
  readonly layer: Layer.Layer<GatewayTransport>;
  /** What each call has done so far, in the order they were opened. */
  readonly calls: () => ReadonlyArray<{readonly open: boolean; readonly released: number}>;
}

export type MakeWorld = (recording: Recording) => TransportWorld;

const TARGET = {host: 'local', method: '/p.S/M', request: Uint8Array.of(7)};
const CAPACITY = 4;
const frames = (count: number): Step[] =>
  Array.from({length: count}, (_, i) => ({frame: Uint8Array.of(i + 1)}));
const played = (...steps: Step[]): Recording => ({respond: () => steps});

const failureTag = <A, E extends {readonly _tag: string}>(exit: Exit.Exit<A, E>) =>
  Exit.isFailure(exit) ? Cause.findErrorOption(exit.cause) : undefined;

const endings = Schema.Literals(['hang', 'close', 'end']);
const kinds = Schema.Literals(['unary', 'stream']);
const count = Schema.Int.check(Schema.isBetween({minimum: 0, maximum: 6}));

const ending = (how: 'hang' | 'close' | 'end'): Step =>
  how === 'hang'
    ? {hang: true}
    : how === 'close'
      ? {close: true}
      : {end: {message: 'lost', status: 14}};

type Run = <A, E>(
  recording: Recording,
  use: (transport: GatewayTransport['Service'], world: TransportWorld) => Effect.Effect<A, E>,
) => Effect.Effect<A, E>;

const unaryCases = (run: Run): void => {
  it.effect('a unary call returns its one reply and releases once', () =>
    run(played(...frames(1)), (transport, world) =>
      Effect.gen(function* () {
        expect(yield* transport.unary(TARGET)).toEqual(Uint8Array.of(1));
        expect(world.calls()).toEqual([{open: false, released: 1}]);
      }),
    ),
  );

  it.effect(
    'a unary call that ends with a status fails with it, and with none when it has none',
    () =>
      run(played({end: {message: 'gone', status: 14}}), (transport, world) =>
        Effect.gen(function* () {
          const error = yield* Effect.flip(transport.unary(TARGET));
          expect(error._tag).toBe('CallEnded');
          expect(error._tag === 'CallEnded' && [error.message, error.status]).toEqual(['gone', 14]);
          expect(world.calls()).toEqual([{open: false, released: 1}]);
        }),
      ),
  );

  it.effect('a unary call closed without a reply fails', () =>
    run(played({close: true}), (transport, world) =>
      Effect.gen(function* () {
        const error = yield* Effect.flip(transport.unary(TARGET));
        expect(error._tag === 'CallEnded' && error.message).toBe('missing unary response');
        expect(world.calls()).toEqual([{open: false, released: 1}]);
      }),
    ),
  );
};

const streamCases = (run: Run): void => {
  it.effect('a stream delivers its frames in order and ends when the call closes', () =>
    run(played(...frames(3), {close: true}), (transport, world) =>
      Effect.gen(function* () {
        const got = yield* Stream.runCollect(transport.stream(TARGET, CAPACITY));
        expect([...got]).toEqual([Uint8Array.of(1), Uint8Array.of(2), Uint8Array.of(3)]);
        expect(world.calls()).toEqual([{open: false, released: 1}]);
      }),
    ),
  );

  it.effect('a stream that ends with a failure delivers the frames before it', () =>
    run(played(...frames(2), {end: {message: 'lost', status: 14}}), (transport, world) =>
      Effect.gen(function* () {
        const got: number[] = [];
        const exit = yield* Effect.exit(
          Stream.runForEach(transport.stream(TARGET, CAPACITY), bytes =>
            Effect.sync(() => got.push(bytes[0] ?? -1)),
          ),
        );
        expect(got).toEqual([1, 2]);
        expect(failureTag(exit)).toMatchObject({value: {_tag: 'CallEnded', status: 14}});
        expect(world.calls()).toEqual([{open: false, released: 1}]);
      }),
    ),
  );

  it.effect('a stream whose reader falls behind fails with StreamOverflow and releases once', () =>
    run(played(...frames(CAPACITY + 3), {hang: true}), (transport, world) =>
      Effect.gen(function* () {
        const exit = yield* Effect.exit(Stream.runDrain(transport.stream(TARGET, CAPACITY)));
        expect(failureTag(exit)).toMatchObject({value: {_tag: 'StreamOverflow'}});
        expect(world.calls()).toEqual([{open: false, released: 1}]);
      }),
    ),
  );
};

const releaseProperty = (run: Run): void => {
  it.effect.prop(
    'a call ends or is given up on in any way and releases its resources exactly once',
    {kind: kinds, delivered: count, how: endings, interrupt: Schema.Boolean},
    ({kind, delivered, how, interrupt}) =>
      run(played(...frames(delivered), ending(how)), (transport, world) =>
        Effect.gen(function* () {
          const call =
            kind === 'unary'
              ? Effect.asVoid(Effect.exit(transport.unary(TARGET)))
              : Effect.asVoid(
                  Effect.exit(Stream.runDrain(transport.stream(TARGET, delivered + 1))),
                );
          const fiber = yield* Effect.forkChild(call);
          yield* settle;
          const [before] = world.calls();
          // A call still open holds its resources; one that ended on its own has released them.
          expect(before?.released).toBe(before?.open === false ? 1 : 0);
          // A call that hangs only ends when it is given up on.
          if (interrupt || how === 'hang') yield* Fiber.interrupt(fiber);
          else yield* Fiber.join(fiber);
          expect(world.calls()).toEqual([{open: false, released: 1}]);
        }),
      ),
    {arbitrary: {runs: 40}},
  );
};

export const gatewayTransportContract = (name: string, make: MakeWorld): void => {
  describe(`${name} as a GatewayTransport`, () => {
    const run: Run = (recording, use) => {
      const world = make(recording);
      return Effect.gen(function* () {
        return yield* use(yield* GatewayTransport, world);
      }).pipe(Effect.provide(world.layer));
    };

    unaryCases(run);
    streamCases(run);
    releaseProperty(run);
  });
};
